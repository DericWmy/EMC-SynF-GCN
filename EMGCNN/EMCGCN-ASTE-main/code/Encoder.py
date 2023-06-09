# -*- coding: utf-8 -*-
import math
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np


class LabelAwareGCN(nn.Module):
    """
    Simple GCN layer
    """
    # dep_dim == pos_dim == 100
    def __init__(self, dep_dim, in_features, out_features, pos_dim=None, bias=True):
        super(LabelAwareGCN, self).__init__()
        self.dep_dim = dep_dim
        self.pos_dim = pos_dim
        self.in_features = in_features
        self.out_features = out_features

        self.dep_attn = nn.Linear(dep_dim + pos_dim + in_features, out_features)
        self.dep_fc = nn.Linear(dep_dim, out_features)
        self.pos_fc = nn.Linear(pos_dim, out_features)

    def forward(self, text, adj, dep_embed, pos_embed=None):
        """

        :param text: [batch size, seq_len, feat_dim]
        :param adj: [batch size, seq_len, seq_len]
        :param dep_embed: [batch size, seq_len, seq_len, dep_type_dim]
        :param pos_embed: [batch size, seq_len, pos_dim]
        :return: [batch size, seq_len, feat_dim]
        """
        # batch_size, seq_len=102, feat_dim=768
        batch_size, seq_len, feat_dim = text.shape
        val_us = text.unsqueeze(dim=2) # [batch_size, seq_len, 1, feat_dim]
        val_us = val_us.repeat(1, 1, seq_len, 1) # [batch_size, seq_len, seq_len, feat_dim]
        pos_us = pos_embed.unsqueeze(dim=2).repeat(1, 1, seq_len, 1) # 2,14,14,100
        # [batch size, seq_len, seq_len, feat_dim+pos_dim+dep_dim]
        val_sum = torch.cat([val_us, pos_us, dep_embed], dim=-1) # 2,14,14,968

        r = self.dep_attn(val_sum) # [batch_size, seq_len,seq_len, feat_dim]

        p = torch.sum(r, dim=-1) # [batch_size, seq_len, seq_len]
        mask = (adj == 0).float() * (-1e30)
        p = p + mask
        p = torch.softmax(p, dim=2)
        p_us = p.unsqueeze(3).repeat(1, 1, 1, feat_dim)

        output = val_us + self.pos_fc(pos_us) + self.dep_fc(dep_embed)
        output = torch.mul(p_us, output) # [batch_size, seq_len, seq_len, feat_dim]

        output_sum = torch.sum(output, dim=2) # [batch_size, seq_len, feat_dim]

        return r, output_sum, p


class nLaGCN(nn.Module):
    def __init__(self, args):
        super(nLaGCN, self).__init__()
        self.args = args
        self.model = nn.ModuleList([LabelAwareGCN(args.dep_dim, args.bert_feature_dim,
                                                  args.bert_feature_dim, args.pos_dim)
                                    for i in range(self.args.num_layer)])
        self.dep_embedding = nn.Embedding(args.deprel_size, args.dep_dim, padding_idx=0)
        # self.dep_embedding = nn.Embedding(args.deprel_size, args.dep_dim, padding_idx=0)

    def forward(self, x, simple_graph, graph, pos_embed=None, output_attention=False):
        # simple_graph = simple_word_pair_deprel, graph = word_pair_deprel
        dep_embed = self.dep_embedding(graph) #[batch_size, seq_len, seq_len, dep_dim]

        attn_list = []
        for lagcn in self.model:
            r, x, attn = lagcn(x, simple_graph, dep_embed, pos_embed=pos_embed)
            attn_list.append(attn)

        if output_attention is True:
            return x, r, attn_list
        else:
            return x, r


class SynFueEncoder(nn.Module):
    def __init__(self, args):
        super(SynFueEncoder, self).__init__()
        self.args = args
        self.lagcn = nLaGCN(args)
        self.fc = nn.Linear(args.bert_feature_dim*2 + args.pos_dim, args.bert_feature_dim)
        self.output_dropout = nn.Dropout(args.output_dropout)
        self.pod_embedding = nn.Embedding(args.postag_ca_size, args.pos_dim, padding_idx=0)

    def forward(self, word_reps, simple_graph, graph, pos=None, output_attention=False):
        """

        :param word_reps: [B, L, H]
        :param simple_graph: [B, L, L]
        :param graph: [B, L, L]
        :param pos: [B, L]
        :param output_attention: bool
        :return:
            output: [B, L, H]
            dep_reps: [B, L, H]
            cls_reps: [B, H]
        """

        pos_embed = self.pod_embedding(pos) # [batch_size, seq_len, pos_dim]
        # simple_graph=[batch_size, seq_len, seq_len]
        lagcn_output = self.lagcn(word_reps, simple_graph, graph, pos_embed, output_attention)
        # pos_output=2,14,100
        pos_output = self.local_attn(word_reps, pos_embed, self.args.num_layer, self.args.w_size)

        output = torch.cat((lagcn_output[0], pos_output, word_reps), dim=-1) # 2,14,1636
        output = self.fc(output) # 2,14,1536
        output = self.output_dropout(output)
        return output, lagcn_output[1]

    def local_attn(self, x, pos_embed, num_layer, w_size):
        """

        :param x:
        :param pos_embed:
        :return:
        """
        batch_size, seq_len, feat_dim = x.shape # batch_size=2,seq_len=14,feat_dim=768
        pos_dim = pos_embed.size(-1) # pos_dim=100
        output = pos_embed # 2,14,100[batch_size, seq_len, pos_dim]
        for i in range(num_layer):
            val_sum = torch.cat([x, output], dim=-1) # [batch size, seq_len, feat_dim+pos_dim]
            attn = torch.matmul(val_sum, val_sum.transpose(1, 2)) # [batch size, seq_len, seq_len]
            # pad size = seq_len + (window_size - 1) // 2 * 2
            pad_size = seq_len + w_size * 2 # 24
            mask = torch.zeros((batch_size, seq_len, pad_size), dtype=torch.float).to(
                device=torch.device('cuda' if torch.cuda.is_available() else 'cpu'))
            for i in range(seq_len):
                mask[:, i, i:i + w_size] = 1.0
            pad_attn = torch.full((batch_size, seq_len, w_size), -1e18).to(
                device=torch.device('cuda' if torch.cuda.is_available() else 'cpu'))
            attn = torch.cat([pad_attn, attn, pad_attn], dim=-1)
            local_attn = torch.softmax(torch.mul(attn, mask), dim=-1)
            local_attn = local_attn[:, :, w_size:pad_size - w_size]  # [batch size, seq_len, seq_len]
            local_attn = local_attn.unsqueeze(dim=3).repeat(1, 1, 1, pos_dim)
            output = output.unsqueeze(dim=2).repeat(1, 1, seq_len, 1)
            output = torch.sum(torch.mul(output, local_attn), dim=2)  # [batch size, seq_len, pos_dim]
        return output
