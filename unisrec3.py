# -*- coding: utf-8 -*-
# @Time    : 2020/9/18 12:08
# @Author  : Hui Wang
# @Email   : hui.wang@ruc.edu.cn

import math
import random
import numpy as np
import torch
from torch import nn
import torch.nn.functional as F
from recbole.model.abstract_recommender import SequentialRecommender
from Layers.layers import TransformerEncoder, HGNN
import copy
from recbole.model.sequential_recommender.sasrec import SASRec


#SOFT , rela-transformer, Nystromformer, conformer, compressive-transformer-pytorch,sinkhorn-transformer, GraphiT,flash_pytorchm Graph_Transformer_Networks, memory-efficient-attention-pytorch


def sim(z1: torch.Tensor, z2: torch.Tensor):
    z1 = F.normalize(z1)
    z2 = F.normalize(z2)
    return torch.matmul(z1, z2.permute(0,2,1))

class UniSRec(SequentialRecommender):

    def __init__(self, config, dataset):
        super(UniSRec, self).__init__(config, dataset)

        self.train_stage = config['train_stage']
        self.temperature = config['temperature']
        self.lam = config['lambda']

        assert self.train_stage in [
            'pretrain', 'inductive_ft', 'transductive_ft'
        ], f'Unknown train stage: [{self.train_stage}]'

        if self.train_stage in ['pretrain', 'inductive_ft']:
            self.item_embedding = None
            # for `transductive_ft`, `item_embedding` is defined in SASRec base model
        if self.train_stage in ['inductive_ft', 'transductive_ft']:
            # `plm_embedding` in pre-train stage will be carried via dataloader
            self.plm_embedding = copy.deepcopy(dataset.plm_embedding)

        # load parameters info
        self.n_layers = config['n_layers']
        self.n_heads = config['n_heads']
        self.hidden_size = config['hidden_size']  # same as embedding_size
        self.inner_size = config['inner_size']  # the dimensionality in feed-forward layer
        self.hidden_dropout_prob = config['hidden_dropout_prob']
        self.attn_dropout_prob = config['attn_dropout_prob']
        self.hidden_act = config['hidden_act']
        self.layer_norm_eps = config['layer_norm_eps']

        self.mask_ratio = config['mask_ratio']

        self.loss_type = config['loss_type']
        self.initializer_range = config['initializer_range']

        self.hglen = config['hyper_len']
        self.enable_hg = config['enable_hg']
        self.enable_ms = config['enable_ms']
        self.dataset = config['dataset']

        #self.buy_type = dataset.field2token_id["item_type_list"]['0']

        # load dataset info
        self.mask_token = self.n_items
        self.mask_item_length = int(self.mask_ratio * self.max_seq_length)

        # define layers and loss
        #self.item_embedding = nn.Embedding(self.n_items, self.hidden_size, padding_idx=0)
        #self.position_embedding = nn.Embedding(self.max_seq_length, self.hidden_size)

        self.plm_size = dataset.plm_size
        self.plm_embedding = copy.deepcopy(dataset.plm_embedding)
        #self.adapter = MLPLayers(config['adapter_layers'])
        
        #self.type_embedding = nn.Embedding(6, self.hidden_size, padding_idx=0)
        self.item_embedding = nn.Embedding(self.n_items + 1, self.hidden_size, padding_idx=0)  # mask token add 1
        self.position_embedding = nn.Embedding(self.max_seq_length + 1, self.hidden_size)  # add mask_token at the last
        if self.enable_ms:
            self.trm_encoder = TransformerEncoder(
                n_layers=self.n_layers,
                n_heads=self.n_heads,
                hidden_size=self.hidden_size,
                inner_size=self.inner_size,
                hidden_dropout_prob=self.hidden_dropout_prob,
                attn_dropout_prob=self.attn_dropout_prob,
                hidden_act=self.hidden_act,
                layer_norm_eps=self.layer_norm_eps,
                multiscale=True,
                scales=config["scales"]
            )
        else:
            self.trm_encoder = TransformerEncoder(
                n_layers=self.n_layers,
                n_heads=self.n_heads,
                hidden_size=self.hidden_size,
                inner_size=self.inner_size,
                hidden_dropout_prob=self.hidden_dropout_prob,
                attn_dropout_prob=self.attn_dropout_prob,
                hidden_act=self.hidden_act,
                layer_norm_eps=self.layer_norm_eps,
                multiscale=False
            )
        self.hgnn_layer = HGNN(self.hidden_size)
        self.LayerNorm = nn.LayerNorm(self.hidden_size, eps=self.layer_norm_eps)
        self.dropout = nn.Dropout(self.hidden_dropout_prob)

        self.hg_type_embedding = nn.Embedding(6, self.hidden_size, padding_idx=0)
        self.metric_w1 = nn.Parameter(torch.Tensor(1, self.hidden_size))
        self.metric_w2 = nn.Parameter(torch.Tensor(1, self.hidden_size))
        self.gating_weight = nn.Parameter(torch.Tensor(self.hidden_size, self.hidden_size))
        self.gating_bias = nn.Parameter(torch.Tensor(1, self.hidden_size))
        self.attn_weights = nn.Parameter(torch.Tensor(self.hidden_size, self.hidden_size))
        self.attn = nn.Parameter(torch.Tensor(1, self.hidden_size))
        nn.init.normal_(self.attn, std=0.02)
        nn.init.normal_(self.attn_weights, std=0.02)
        # nn.init.normal_(self.gating_bias, std=0.02)
        nn.init.normal_(self.gating_weight, std=0.02)
        nn.init.normal_(self.metric_w1, std=0.02)
        nn.init.normal_(self.metric_w2, std=0.02)

       # if self.dataset == "retail_beh":
           # self.sw_before = 10
            #self.sw_follow = 6
        #elif self.dataset == "ijcai_beh":
            #self.sw_before = 30
            #self.sw_follow = 18
       # elif self.dataset == "tmall_beh":
            #self.sw_before = 20
            #self.sw_follow = 12

        self.hypergraphs = dict()
        # we only need compute the loss at the masked position
        try:
            assert self.loss_type in ['BPR', 'CE']
        except AssertionError:
            raise AssertionError("Make sure 'loss_type' in ['BPR', 'CE']!")

        # parameters initialization
        self.apply(self._init_weights)

    def _init_weights(self, module):
        """ Initialize the weights """
        if isinstance(module, (nn.Linear, nn.Embedding)):
            # Slightly different from the TF version which uses truncated_normal for initialization
            # cf https://github.com/pytorch/pytorch/pull/5617
            module.weight.data.normal_(mean=0.0, std=self.initializer_range)
        elif isinstance(module, nn.LayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)
        if isinstance(module, nn.Linear) and module.bias is not None:
            module.bias.data.zero_()

    def get_attention_mask(self, item_seq):
        """Generate bidirectional attention mask for multi-scale attention."""
        if self.enable_ms:
            attention_mask = (item_seq > 0).long()
            extended_attention_mask = attention_mask.unsqueeze(1)
            return extended_attention_mask
        else:
            """Generate bidirectional attention mask for multi-head attention."""
            attention_mask = (item_seq > 0).long()
            extended_attention_mask = attention_mask.unsqueeze(1).unsqueeze(2)  # torch.int64
            # bidirectional mask
            extended_attention_mask = extended_attention_mask.to(dtype=next(self.parameters()).dtype)  # fp16 compatibility
            extended_attention_mask = (1.0 - extended_attention_mask) * -10000.0
            return extended_attention_mask

    def _padding_sequence(self, sequence, max_length):
        pad_len = max_length - len(sequence)
        sequence = [0] * pad_len + sequence
        sequence = sequence[-max_length:]  # truncate according to the max_length
        return sequence
    
    #def reconstruct_train_data(self, item_seq, type_seq, last_buy):
    def reconstruct_train_data(self, item_seq):
        """
        Mask item sequence for training.
        """
        #last_buy = last_buy.tolist()
        device = item_seq.device
        batch_size = item_seq.size(0)

        zero_padding = torch.zeros(item_seq.size(0), dtype=torch.long, device=item_seq.device)
        item_seq = torch.cat((item_seq, zero_padding.unsqueeze(-1)), dim=-1)  # [B max_len+1]
        #type_seq = torch.cat((type_seq, zero_padding.unsqueeze(-1)), dim=-1)
        n_objs = (torch.count_nonzero(item_seq, dim=1)+1).tolist()
        for batch_id in range(batch_size):
            n_obj = n_objs[batch_id]
            #item_seq[batch_id][n_obj-1] = last_buy[batch_id]
            #type_seq[batch_id][n_obj-1] = self.buy_type

        sequence_instances = item_seq.cpu().numpy().tolist()
        #type_instances = type_seq.cpu().numpy().tolist()

        # Masked Item Prediction
        # [B * Len]
        masked_item_sequence = []
        pos_items = []
        masked_index = []

        for instance_idx, instance in enumerate(sequence_instances):
            # WE MUST USE 'copy()' HERE!
            masked_sequence = instance.copy()
            pos_item = []
            index_ids = []
            for index_id, item in enumerate(instance):
                # padding is 0, the sequence is end
                if index_id == n_objs[instance_idx]-1:
                    pos_item.append(item)
                    masked_sequence[index_id] = self.mask_token
                    #type_instances[instance_idx][index_id] = 0
                    index_ids.append(index_id)
                    break
                prob = random.random()
                if prob < self.mask_ratio:
                    pos_item.append(item)
                    masked_sequence[index_id] = self.mask_token
                    #type_instances[instance_idx][index_id] = 0
                    index_ids.append(index_id)

            masked_item_sequence.append(masked_sequence)
            pos_items.append(self._padding_sequence(pos_item, self.mask_item_length))
            masked_index.append(self._padding_sequence(index_ids, self.mask_item_length))

        # [B Len]
        masked_item_sequence = torch.tensor(masked_item_sequence, dtype=torch.long, device=device).view(batch_size, -1)
        # [B mask_len]
        pos_items = torch.tensor(pos_items, dtype=torch.long, device=device).view(batch_size, -1)
        # [B mask_len]
        masked_index = torch.tensor(masked_index, dtype=torch.long, device=device).view(batch_size, -1)
        #type_instances = torch.tensor(type_instances, dtype=torch.long, device=device).view(batch_size, -1)
        #return masked_item_sequence, pos_items, masked_index, type_instances
        return masked_item_sequence, pos_items, masked_index

    def reconstruct_test_data(self, item_seq, item_seq_len):
        """
        Add mask token at the last position according to the lengths of item_seq
        """
        padding = torch.zeros(item_seq.size(0), dtype=torch.long, device=item_seq.device)  # [B]
        item_seq = torch.cat((item_seq, padding.unsqueeze(-1)), dim=-1)  # [B max_len+1]
        #item_type = torch.cat((item_type, padding.unsqueeze(-1)), dim=-1)
        for batch_id, last_position in enumerate(item_seq_len):
            item_seq[batch_id][last_position] = self.mask_token
        return item_seq

    def forward(self, item_seq, mask_positions_nums=None, session_id=None):
        position_ids = torch.arange(item_seq.size(1), dtype=torch.long, device=item_seq.device)
        position_ids = position_ids.unsqueeze(0).expand_as(item_seq)
        position_embedding = self.position_embedding(position_ids)
        #type_embedding = self.type_embedding(type_seq)
        item_emb = self.item_embedding(item_seq)
        input_emb = item_emb + position_embedding
        input_emb = self.LayerNorm(input_emb)
        input_emb = self.dropout(input_emb)
        extended_attention_mask = self.get_attention_mask(item_seq)
        trm_output = self.trm_encoder(input_emb, extended_attention_mask, output_all_encoded_layers=True)
        output = trm_output[-1]

        if self.enable_hg:
            x_raw = item_emb
            x_raw = x_raw * torch.sigmoid(x_raw.matmul(self.gating_weight)+self.gating_bias)
            # b, l, l
            x_m = torch.stack((self.metric_w1*x_raw, self.metric_w2*x_raw)).mean(0)
            item_sim = sim(x_m, x_m)
            item_sim[item_sim < 0] = 0.01

            Gs = self.build_Gs_unique(item_seq, item_sim, self.hglen)
            # Gs = self.build_Gs_light(item_seq, item_sim, self.hglen)

            batch_size = item_seq.shape[0]
            seq_len = item_seq.shape[1]
            n_objs = torch.count_nonzero(item_seq, dim=1)
            indexed_embs = list()
            for batch_idx in range(batch_size):
                n_obj = n_objs[batch_idx]
                # l', dim
                indexed_embs.append(x_raw[batch_idx][:n_obj])
            indexed_embs = torch.cat(indexed_embs, dim=0)
            hgnn_embs = self.hgnn_layer(indexed_embs, Gs)
            hgnn_take_start = 0
            hgnn_embs_padded = []
            for batch_idx in range(batch_size):
                n_obj = n_objs[batch_idx]
                embs = hgnn_embs[hgnn_take_start:hgnn_take_start+n_obj]
                hgnn_take_start += n_obj
                # l', dim || padding emb -> l, dim
                padding = torch.zeros((seq_len-n_obj, embs.shape[-1])).to(item_seq.device)
                embs = torch.cat((embs, padding), dim=0)
                if mask_positions_nums is not None:
                    mask_len = mask_positions_nums[1][batch_idx]
                    poss = mask_positions_nums[0][batch_idx][-mask_len:].tolist()
                    for pos in poss:
                        if pos == 0:
                            continue
                        # if pos<n_obj-1:
                        #     readout = torch.mean(torch.cat((embs[:pos], embs[pos+1:]), dim=0), dim=0)
                        # else:
                        sliding_window_start = pos-self.sw_before if pos-self.sw_before>-1 else 0
                        sliding_window_end = pos+self.sw_follow if pos+self.sw_follow<n_obj else n_obj-1
                        readout = torch.mean(torch.cat((embs[sliding_window_start:pos], embs[pos+1:sliding_window_end]), dim=0),dim=0)
                        embs[pos] = readout
                else:
                    pos = (item_seq[batch_idx]==self.mask_token).nonzero(as_tuple=True)[0][0]
                    sliding_window_start = pos-self.sw_before if pos-self.sw_before>-1 else 0
                    embs[pos] = torch.mean(embs[sliding_window_start:pos], dim=0)
                hgnn_embs_padded.append(embs)
            # b, l, dim
            hgnn_embs = torch.stack(hgnn_embs_padded, dim=0)
            # x = x_raw
            # 2, b, l, dim
            mixed_x = torch.stack((output, hgnn_embs), dim=0)
            weights = (torch.matmul(mixed_x, self.attn_weights.unsqueeze(0).unsqueeze(0))*self.attn).sum(-1)
            # 2, b, l, 1
            score = F.softmax(weights, dim=0).unsqueeze(-1)
            mixed_x = (mixed_x*score).sum(0)
            # mixed_x = self.bert.forward_from_emb(tokens, beh_types, mixed_x)
            # b, s, n
            # mixed_x = self.out(mixed_x)
            assert not torch.isnan(mixed_x).any()

            # mask_pos = item_seq==self.mask_token
            # attn_score_mask = score.squeeze().permute(1,2,0)[mask_pos]
            # attn_score_mask = score.squeeze().permute(1,2,0)
            # out_self_attn = torch.matmul(output, output.transpose(-2,-1)) / math.sqrt(output.size(-1))
            # out_self_attn = F.softmax(out_self_attn, -1)
            return mixed_x
        return output  # [B L H]

    def multi_hot_embed(self, masked_index, max_length):
        """
        For memory, we only need calculate loss for masked position.
        Generate a multi-hot vector to indicate the masked position for masked sequence, and then is used for
        gathering the masked position hidden representation.
        Examples:
            sequence: [1 2 3 4 5]
            masked_sequence: [1 mask 3 mask 5]
            masked_index: [1, 3]
            max_length: 5
            multi_hot_embed: [[0 1 0 0 0], [0 0 0 1 0]]
        """
        masked_index = masked_index.view(-1)
        multi_hot = torch.zeros(masked_index.size(0), max_length, device=masked_index.device)
        multi_hot[torch.arange(masked_index.size(0)), masked_index] = 1
        return multi_hot

    def pretrain(self, interaction):
        item_seq = interaction[self.ITEM_SEQ]
        item_seq_len = interaction[self.ITEM_SEQ_LEN]
        #item_emb_list = self.moe_adaptor(interaction['item_emb_list'])
        seq_output = self.forward(item_seq, item_seq_len)
        seq_output = F.normalize(seq_output, dim=1)

        # Remove sequences with the same next item
        pos_id = interaction['item_id']
        same_pos_id = (pos_id.unsqueeze(1) == pos_id.unsqueeze(0))
        same_pos_id = torch.logical_xor(same_pos_id, torch.eye(pos_id.shape[0], dtype=torch.bool, device=pos_id.device))

        loss_seq_item = self.seq_item_contrastive_task(seq_output, same_pos_id, interaction)
        loss_seq_seq = self.seq_seq_contrastive_task(seq_output, same_pos_id, interaction)
        loss = loss_seq_item + self.lam * loss_seq_seq
        return loss

    def calculate_loss(self, interaction):
        if self.train_stage == 'pretrain':
            return self.pretrain(interaction)

        # Loss for fine-tuning
        item_seq = interaction[self.ITEM_SEQ]
        item_seq_len = interaction[self.ITEM_SEQ_LEN]
        #item_emb_list = self.moe_adaptor(self.plm_embedding(item_seq))
        seq_output = self.forward(item_seq, item_seq_len)
        test_item_emb = self.item_embedding.weight  # [item_num H]
        if self.train_stage == 'transductive_ft':
            test_item_emb = test_item_emb + self.item_embedding.weight

        seq_output = F.normalize(seq_output, dim=1)
        test_item_emb = F.normalize(test_item_emb, dim=1)

        logits = torch.matmul(seq_output, test_item_emb.transpose(0, 1)) / self.temperature
        pos_items = interaction[self.POS_ITEM_ID]
        loss = self.loss_fct(logits, pos_items)
        return loss
    
    def predict(self, interaction):
        item_seq = interaction[self.ITEM_SEQ]
        item_seq_len = interaction[self.ITEM_SEQ_LEN]
        test_item = interaction[self.ITEM_ID]
        seq_output = self.forward(item_seq, item_seq_len)
        test_item_emb = self.item_embedding(test_item)
        scores = torch.mul(seq_output, test_item_emb).sum(dim=1)  # [B]
        return scores
    
    def full_sort_predict(self, interaction):
        item_seq = interaction['item_id_list']
        #type_seq = interaction['item_type_list']
        item_seq_len = torch.count_nonzero(item_seq, 1)
        item_seq = self.reconstruct_test_data(item_seq, item_seq_len)
        seq_output = self.forward(item_seq, item_seq_len)
        seq_output = self.gather_indexes(seq_output, item_seq_len)  # [B H]
        test_items_emb = self.item_embedding.weight[:self.n_items]  # delete masked token
        scores = torch.matmul(seq_output, test_items_emb.transpose(0, 1))  # [B, item_num]
        return scores

    def build_Gs_unique(self, seqs, item_sim, group_len):
        Gs = []
        n_objs = torch.count_nonzero(seqs, dim=1).tolist()
        for batch_idx in range(seqs.shape[0]):
            seq = seqs[batch_idx]
            n_obj = n_objs[batch_idx]
            seq = seq[:n_obj].cpu()
            seq_list = seq.tolist()
            unique = torch.unique(seq)
            unique = unique.tolist()
            n_unique = len(unique)

            multibeh_group = seq.tolist()
            for x in unique:
                multibeh_group.remove(x)
            multibeh_group = list(set(multibeh_group))
            try:
                multibeh_group.remove(self.mask_token)
            except:
                pass
            
            # l', l'
            seq_item_sim = item_sim[batch_idx][:n_obj, :][:, :n_obj]            
            # l', group_len
            if group_len>n_obj:
                metrics, sim_items = torch.topk(seq_item_sim, n_obj, sorted=False)
            else:
                metrics, sim_items = torch.topk(seq_item_sim, group_len, sorted=False)
            # map indices to item tokens
            sim_items = seq[sim_items]
            row_idx, masked_pos = torch.nonzero(sim_items==self.mask_token, as_tuple=True)
            sim_items[row_idx, masked_pos] = seq[row_idx]
            metrics[row_idx, masked_pos] = 1.0
            # print(sim_items.detach().cpu().tolist())
            multibeh_group = seq.tolist()
            for x in unique:
                multibeh_group.remove(x)
            multibeh_group = list(set(multibeh_group))
            try:
                multibeh_group.remove(self.mask_token)
            except:
                pass
            n_edge = n_unique+len(multibeh_group)
            # hyper graph: n_obj, n_edge
            H = torch.zeros((n_obj, n_edge), device=metrics.device)
            normal_item_indexes = torch.nonzero((seq != self.mask_token), as_tuple=True)[0]
            for idx in normal_item_indexes:
                sim_items_i = sim_items[idx].tolist()
                map_f = lambda x: unique.index(x)
                unique_idx = list(map(map_f, sim_items_i))
                H[idx, unique_idx] = metrics[idx]

            for i, item in enumerate(seq_list):
                ego_idx = unique.index(item)
                H[i, ego_idx] = 1.0
                # multi-behavior hyperedge
                if item in multibeh_group:
                    H[i, n_unique+multibeh_group.index(item)] = 1.0
            # print(H.detach().cpu().tolist())
            # W = torch.ones(n_edge, device=H.device)
            # W = torch.diag(W)
            DV = torch.sum(H, dim=1)
            DE = torch.sum(H, dim=0)
            invDE = torch.diag(torch.pow(DE, -1))
            invDV = torch.diag(torch.pow(DV, -1))
            # DV2 = torch.diag(torch.pow(DV, -0.5))
            HT = H.t()
            G = invDV.mm(H).mm(invDE).mm(HT)
            # G = DV2.mm(H).mm(invDE).mm(HT).mm(DV2)
            assert not torch.isnan(G).any()
            Gs.append(G.to(seqs.device))
        Gs_block_diag = torch.block_diag(*Gs)
        return Gs_block_diag
