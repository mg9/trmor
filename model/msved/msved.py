# -----------------------------------------------------------
# Date:        2021/12/19 
# Author:      Muge Kural
# Description: Character-based Variational Autoencoder 
# -----------------------------------------------------------

from functools import reduce
import math
from pprint import pprint
from tokenize import Ignore
import torch
import torch.nn as nn
import numpy as np
from common.utils import log_sum_exp
from torch.nn import functional as F

class MSVED_Encoder(nn.Module):
    """ LSTM Encoder with constant-length batching"""
    def __init__(self, args, embed, model_init, emb_init, bidirectional=True):
        super(MSVED_Encoder, self).__init__()
        self.ni = args.ni
        self.nh = args.enc_nh
        self.nz = args.nz
        self.embed = embed

        self.gru = nn.GRU(input_size=args.ni,
                            hidden_size=args.enc_nh,
                            num_layers=1,
                            batch_first=True,
                            dropout=0,
                            bidirectional=bidirectional)

        self.dropout_in = nn.Dropout(args.enc_dropout_in)

        # dimension transformation to z
        if self.gru.bidirectional:
            self.linear = nn.Linear(args.enc_nh*2, 2*args.nz, bias=True)
        else:
            self.linear = nn.Linear(args.enc_nh,  2*args.nz, bias=True)

        self.reset_parameters(model_init, emb_init)
        #nn.init.xavier_normal_(self.linear.weight)

    def reset_parameters(self, model_init, emb_init):
        for param in self.parameters():
            model_init(param)
        emb_init(self.embed.weight)


    def forward(self, input):
        # (batch_size, seq_len-1, args.ni)
        word_embed = self.embed(input)
        word_embed = self.dropout_in(word_embed)

        _, last_state = self.gru(word_embed)
        if self.gru.bidirectional:
            last_state = torch.cat([last_state[-2], last_state[-1]], 1).unsqueeze(0)
        mean, logvar = self.linear(last_state).chunk(2, -1)
        # (batch_size, 1, enc_nh)
        last_state = last_state.permute(1,0,2)
        return mean.squeeze(0), logvar.squeeze(0), last_state
     

class MSVED_Decoder(nn.Module):
    """LSTM decoder with constant-length batching"""
    def __init__(self, args, embed, vocab, model_init, emb_init):
        super(MSVED_Decoder, self).__init__()
        self.ni = args.ni
        self.nh = args.dec_nh
        self.nz = args.nz
        self.vocab = vocab
        self.device = args.device
        self.char_embed = embed

        # no padding when setting padding_idx to -1
        #self.char_embed = nn.Embedding(len(vocab.word2id), 300, padding_idx=0)

        self.dropout_in = nn.Dropout(args.dec_dropout_in)

        # concatenate z with input
        self.gru = nn.GRU(input_size=650, # self.char_embed+ self.ni
                            hidden_size=256,
                            num_layers=1,
                            batch_first=True)

        self.attn = nn.Linear(300+ 150+ 256, 11)
        self.attn_combine = nn.Linear(650, 650)

        # prediction layer
        self.pred_linear = nn.Linear(args.dec_nh, len(vocab.word2id), bias=True)
        vocab_mask = torch.ones(len(vocab.word2id))
        self.loss = nn.CrossEntropyLoss(weight=vocab_mask, reduce=False, ignore_index=0)
        self.reset_parameters(model_init, emb_init)

    def reset_parameters(self, model_init, emb_init):
        for param in self.parameters():
            model_init(param)
        emb_init(self.char_embed.weight)
        #    torch.nn.init.xavier_normal_(param)

    def forward(self, input, z, hidden, tag_embeddings, tag_attention_masks):
        # input: (batch_size,1), hidden(1,batch_size,hout)
        batch_size, _, _ = z.size()
        seq_len = input.size(1)
        # (batch_size, seq_len, ni)
        embedded = self.char_embed(input)
        embedded = self.dropout_in(embedded)
        z_ = z.expand(batch_size, seq_len, self.nz)

        # (batch_size, 1, ni+nz)
        embedded = torch.cat((embedded, z_), -1)

        # (batchsize,1, ni+nz+hiddensize)
        # Attention Queries
        to_attend = torch.cat((embedded, torch.permute(hidden, (1,0,2))), 2) 

        # (batchsize,1, num_tags)
        # Attention Keys: self.attn
        attention_scores = self.attn(to_attend)
        attention_scores = attention_scores.masked_fill(tag_attention_masks, -1e9)
        attn_weights = F.softmax(attention_scores, dim=2)
       
        # (batchsize, 1, 200)
        attn_applied = torch.bmm(attn_weights,tag_embeddings)
        # (batchsize,1, ni+nz+tagsize)
        output = torch.cat((embedded, attn_applied), 2)
        output = self.attn_combine(output)
        output = F.relu(output)
        #output = torch.tanh(output)

        output, hidden = self.gru(output, hidden)
        # (batch_size, 1, vocab_size)
        output_logits = self.pred_linear(output)
        return output_logits, hidden, attn_weights


class MSVED(nn.Module):
    def __init__(self, args, surf_vocab, tag_vocabs, model_init, emb_init):
        super(MSVED, self).__init__()
        self.embed = nn.Embedding(len(surf_vocab.word2id), args.ni)
        self.encoder = MSVED_Encoder(args, self.embed, model_init, emb_init)
        self.decoder = MSVED_Decoder(args, self.embed, surf_vocab, model_init, emb_init)

        self.args = args
        self.nz = args.nz
        self.tag_embed_dim = 200
        self.dec_nh = 256
        self.char_emb_dim = 300
        self.z_to_dec = nn.Linear(self.nz, 256)
        self.tag_to_dec = nn.Linear(self.tag_embed_dim, 256)

        #torch.nn.init.xavier_uniform(self.z_to_dec.weight)
        #torch.nn.init.xavier_uniform(self.tag_to_dec.weight)

        loc = torch.zeros(self.nz, device=args.device)
        scale = torch.ones(self.nz, device=args.device)
        self.prior = torch.distributions.normal.Normal(loc, scale)
        self.tag_embeddings = nn.ModuleList([])
        self.classifiers = nn.ModuleList([])
        self.tag_embeddings_biases = []
        self.priors = []

        # Discriminative classifiers for q(y|x)
        for key,keydict in tag_vocabs.items():
            self.classifiers.append(nn.Linear(256*2, len(keydict)))
            #nn.init.xavier_normal_(self.classifiers[-1].weight)
            self.priors.append(torch.zeros(1,len(keydict)))

        for key,keydict in tag_vocabs.items():
            print(key, len(keydict))
            self.tag_embeddings.append(nn.Embedding(len(keydict), self.tag_embed_dim))
            self.tag_embeddings_biases.append(nn.Parameter(torch.ones(1,self.tag_embed_dim)).to('cuda'))

   
    def classifier_loss(self, enc_nh, tmp, case=None,polar=None,mood=None,evid=None,pos=None,per=None,num=None,tense=None,aspect=None,inter=None,poss=None):
        sft = nn.Softmax(dim=2)
        #loss = nn.CrossEntropyLoss(reduce=False, ignore_index=0)
        loss = nn.CrossEntropyLoss()
        # (enc_nh: batchsize,1, 256*2)
        tags = [case,polar,mood,evid,pos,per,num,tense,aspect,inter,poss]
            
        preds =[]
        xloss = torch.tensor(0.0).to('cuda')
        gumbel_tag_embeddings = []
        gumbel_logits = []
        for i in range(len(self.classifiers)):
            # (batchsize,1,tagvocabsize)
            logits = self.classifiers[i](enc_nh)
            logits = torch.tanh(logits)
            if tags[i] is not None:
                xloss+=(loss(logits.squeeze(1), tags[i].squeeze(1)))
            preds.append(torch.argmax(sft(logits),dim=2))
            # (batchsize,tagvocabsize)
            _gumbel_logits = F.gumbel_softmax(logits, tau=tmp, hard=False).squeeze(1)
            #tag_embed = (self.tag_embeddings[i].weight + self.tag_embeddings_biases[i])
            tag_embed = self.tag_embeddings[i].weight 
            gumbel_tag_embeddings.append(torch.matmul(_gumbel_logits, tag_embed).unsqueeze(1))
            gumbel_logits.append(_gumbel_logits)
        
        tag_correct = 0; tag_total = 0

        # Data is labeled, so calculate the classification loss
        if tags[0] is not None: 
            # (batchsize, 11)
            #xloss = torch.stack(xloss).t()
            # (batchsize)
            #xloss = torch.sum(xloss,dim=1)
            for j in range(len(tags)):
                tag_correct +=  (preds[i] == tags[i]).sum().item()
                tag_total   +=  len(preds[i])
        else:
            xloss = torch.tensor(0.0).to('cuda')
        # (batchsize,11,tag_embed_dim)
        gumbel_tag_embeddings = torch.cat(gumbel_tag_embeddings, dim=1)
        return  gumbel_logits, gumbel_tag_embeddings, xloss, tag_correct, tag_total

    def loss_labeled_msved(self, lx_src, case,polar,mood,evid,pos,per,num,tense,aspect,inter,poss, lx_tgt, kl_weight, tmp, mode='train'):
        labeled_msved_loss, labeled_pred_loss, tag_correct, tag_total, labeled_recon_loss, labeled_kl_loss, labeled_recon_acc = self.labeled_msved_loss(lx_src, case,polar,mood,evid,pos,per,num,tense,aspect,inter,poss, lx_tgt, kl_weight, tmp, mode=mode)
        return labeled_msved_loss, labeled_pred_loss, tag_correct, tag_total, labeled_recon_loss, labeled_kl_loss, labeled_recon_acc

    def loss_lxtgt_to_lxsrc_msved(self, lx_src, lx_tgt, kl_weight, tmp, mode='train'):
        msved_loss, recon_loss, kl_loss, recon_acc = self.msved_loss(lx_src, lx_tgt, kl_weight, tmp, mode=mode)
        return msved_loss, recon_loss, kl_loss, recon_acc

    def loss_lxsrc_msvae(self, lx_src, kl_weight, tmp, mode='train'):
        msvae_loss, recon_loss, kl_loss, recon_acc, gumbel_classes  = self.msvae_loss(lx_src, kl_weight, tmp, mode=mode)
        return msvae_loss, recon_loss, kl_loss, recon_acc, gumbel_classes

    def loss_lxtgt_labeled_msvae(self, lx_tgt, case,polar,mood,evid,pos,per,num,tense,aspect,inter,poss, kl_weight, tmp, mode='train'):
        labeled_msved_loss, labeled_pred_loss, tag_correct, tag_total, labeled_recon_loss, labeled_kl_loss, labeled_recon_acc = self.labeled_msvae_loss(lx_tgt, case,polar,mood,evid,pos,per,num,tense,aspect,inter,poss, kl_weight, tmp, mode=mode)
        return labeled_msved_loss, labeled_pred_loss, tag_correct, tag_total, labeled_recon_loss, labeled_kl_loss, labeled_recon_acc

    def loss_ux_msvae(self, ux, kl_weight, tmp, mode='train'):
        # a * [U(x)] + [Lu (xs|xt)] + [Ll (xt, yt| xs) - D(xt|yt)]
        # [U(x)]
        msvae_loss, recon_loss, kl_loss, recon_acc, gumbel_classes = self.msvae_loss(ux, kl_weight, tmp, mode= mode)
        return msvae_loss, recon_loss, kl_loss, recon_acc, gumbel_classes

    def msvae_loss(self, x,  kl_weight, tmp, mode='train'):
        # Lu (xs|xs)
        mu, logvar, encoder_fhs = self.encoder(x)
        if mode == 'train':
            # (batchsize, 1, nz)
            z = self.reparameterize(mu, logvar)
        else:
            z = mu.unsqueeze(0)
        
        # gumbel_tag_embeddings: (batchsize, 11, tag_embed_size)
        gumbel_logits, gumbel_tag_embeddings, _, _, _ = self.classifier_loss(encoder_fhs, tmp)
        sft = nn.Softmax(dim=1)
        tag_att_masks = []
        for i in range(len(gumbel_logits)):
            tag_att_masks.append(torch.argmax(gumbel_logits[i],dim=1) == 0)
        # (batchsize, 1, 11)
        tag_att_masks = (torch.stack(tag_att_masks).t()).unsqueeze(1)


        # (batchsize, 1, tag_emb_dim)
        tag_all_embed = torch.sum(gumbel_tag_embeddings,dim=1).unsqueeze(1)
        #TODO: add bias
        tag_all_embed = torch.tanh(tag_all_embed)
        dec_h0 = torch.tanh(self.tag_to_dec(tag_all_embed) + self.z_to_dec(z))
        dec_h0 = torch.permute(dec_h0, (1,0,2))

        if mode == 'train':
            recon_loss, recon_acc = self.recon_loss(x, z, dec_h0, gumbel_tag_embeddings, tag_att_masks, recon_type='sum')
        else:
            recon_loss, recon_acc, _ = self.recon_loss_test(x, z, dec_h0, gumbel_tag_embeddings, tag_att_masks, recon_type='sum')

        # (batchsize)
        kl_loss = self.kl_loss(mu,logvar)

        # (batchsize)
        recon_loss = recon_loss.squeeze(1)#.mean()

        log_py_prior = self.log_py_prior_w_gumbels(gumbel_logits)

        #loss = log_py_prior + recon_loss + kl_weight * kl_loss
        loss =  recon_loss + kl_weight * kl_loss
        return loss, recon_loss, kl_loss, recon_acc, self.gumbel_classes(gumbel_logits)

    def gumbel_classes(self, gumbel_logits):
        probs = []
        for i in range(len(gumbel_logits)):
            #probs.append(torch.softmax(gumbel_logits[i],dim=1))
            probs.append(gumbel_logits[i])
        
        return probs

    def msved_loss(self, x, reinflect_surf, kl_weight, tmp, mode='train'):
        mu, logvar, encoder_fhs = self.encoder(reinflect_surf)
        _, _, xt_encoder_fhs = self.encoder(x)

        if mode == 'train':
            # (batchsize, 1, nz)
            z = self.reparameterize(mu, logvar)
        else:
            z = mu.unsqueeze(0)

        # gumbel_tag_embeddings: (batchsize, 11, tag_embed_size)
        gumbel_logits, gumbel_tag_embeddings, _, _, _  = self.classifier_loss(xt_encoder_fhs, tmp)
        sft = nn.Softmax(dim=1)
        tag_att_masks = []
        for i in range(len(gumbel_logits)):
            tag_att_masks.append(torch.argmax(gumbel_logits[i],dim=1) == 0)
        # (batchsize, 1, 11)
        tag_att_masks = (torch.stack(tag_att_masks).t()).unsqueeze(1)

        # (batchsize, 1, tag_emb_dim)
        tag_all_embed = torch.sum(gumbel_tag_embeddings,dim=1).unsqueeze(1)
        #TODO: add bias
        tag_all_embed = torch.tanh(tag_all_embed)
        dec_h0 = torch.tanh(self.tag_to_dec(tag_all_embed) + self.z_to_dec(z))
        dec_h0 = torch.permute(dec_h0, (1,0,2))

    
        if mode == 'train':
            recon_loss, recon_acc = self.recon_loss(x, z, dec_h0, gumbel_tag_embeddings, tag_att_masks, recon_type='sum')
        else:
            recon_loss, recon_acc = self.recon_loss_test(x, z, dec_h0, gumbel_tag_embeddings, tag_att_masks, recon_type='sum')

        # (batchsize)
        kl_loss = self.kl_loss(mu,logvar)

        # (batchsize)
        recon_loss = recon_loss.squeeze(1)#.mean()

        log_py_prior = self.log_py_prior_w_gumbels(gumbel_logits)

        #loss = log_py_prior + recon_loss+ kl_weight * kl_loss
        loss =  recon_loss+ kl_weight * kl_loss

        return loss, recon_loss, kl_loss, recon_acc
    
    def labeled_msved_loss(self, x, case,polar,mood,evid,pos,per,num,tense,aspect,inter,poss, reinflect_surf, kl_weight, tmp, mode='train'):
        # Ll (xt, yt | xs)
        mu, logvar, encoder_fhs = self.encoder(x)
        _, _, xt_encoder_fhs = self.encoder(reinflect_surf)
        _, _, xloss, tag_correct, tag_total = self.classifier_loss(xt_encoder_fhs, tmp, case,polar,mood,evid,pos,per,num,tense,aspect,inter,poss)

        if mode == 'train':
            # (batchsize, 1, nz)
            z = self.reparameterize(mu, logvar)
        else:
            z = mu.unsqueeze(1)

        #(batchsize,1,tag_embed_dim)
        tags = [case,polar,mood,evid,pos,per,num,tense,aspect,inter,poss]
        embeds =[]
        tag_attention_masks = []
        for i in range(len(tags)):
            tag_attention_masks.append(tags[i]==0)
            # (batchsize,1, tag_embed_dim)
            tag_mask = (tags[i]!=0).unsqueeze(1).repeat(1,1,self.tag_embed_dim)
            tag_emb = self.tag_embeddings[i](tags[i])
            embed = tag_mask * tag_emb #+ self.tag_embeddings_biases[i])
            embeds.append(embed)
        # (batchsize, 11, tag_emb_dim)
        tag_embeddings = torch.cat(embeds,dim=1)
        # (batchsize, 1, 11)
        tag_attention_masks = torch.permute(torch.stack(tag_attention_masks), (1,2,0))
        # (batchsize, 1, tag_emb_dim)
        tag_all_embed = torch.sum(torch.cat(embeds,dim=1),dim=1).unsqueeze(1)
        #TODO: add bias
        tag_all_embed = torch.tanh(tag_all_embed)
        dec_h0 = torch.tanh(self.tag_to_dec(tag_all_embed) + self.z_to_dec(z))
        dec_h0 = torch.permute(dec_h0, (1,0,2))
        if mode == 'train':
            recon_loss, recon_acc = self.recon_loss(reinflect_surf, z, dec_h0, tag_embeddings, tag_attention_masks, recon_type='sum')
        else:
            recon_loss, recon_acc, _ = self.recon_loss_test(reinflect_surf, z, dec_h0, tag_embeddings, tag_attention_masks, recon_type='sum')
       
        log_py_prior = self.log_py_prior(case,polar,mood,evid,pos,per,num,tense,aspect,inter,poss)

        # (batchsize)
        kl_loss = self.kl_loss(mu,logvar)
       
        # (batchsize)
        recon_loss = recon_loss.squeeze(1)#.mean()
        #loss = log_py_prior + xloss + recon_loss + kl_weight * kl_loss
        loss =  xloss + recon_loss + kl_weight * kl_loss
        
        return loss, xloss, tag_correct, tag_total, recon_loss, kl_loss, recon_acc

    def labeled_msvae_loss(self, reinflect_surf, case,polar,mood,evid,pos,per,num,tense,aspect,inter,poss, kl_weight, tmp, mode='train'):
        # Ll (xt, yt | xs)
        mu, logvar, encoder_fhs = self.encoder(reinflect_surf)
        _, _, xloss, tag_correct, tag_total = self.classifier_loss(encoder_fhs, tmp, case,polar,mood,evid,pos,per,num,tense,aspect,inter,poss)

        if mode == 'train':
            # (batchsize, 1, nz)
            z = self.reparameterize(mu, logvar)
        else:
            z = mu.unsqueeze(1)

        #(batchsize,1,tag_embed_dim)
        tags = [case,polar,mood,evid,pos,per,num,tense,aspect,inter,poss]
        embeds =[]
        tag_attention_masks = []
        for i in range(len(tags)):
            tag_attention_masks.append(tags[i]==0)
            # (batchsize,1, tag_embed_dim)
            tag_mask = (tags[i]!=0).unsqueeze(1).repeat(1,1,self.tag_embed_dim)
            tag_emb = self.tag_embeddings[i](tags[i])
            embed = tag_mask * tag_emb #+ self.tag_embeddings_biases[i])
            embeds.append(embed)
        # (batchsize, 11, tag_emb_dim)
        tag_embeddings = torch.cat(embeds,dim=1)
        # (batchsize, 1, 11)
        tag_attention_masks = torch.permute(torch.stack(tag_attention_masks), (1,2,0))
        # (batchsize, 1, tag_emb_dim)
        tag_all_embed = torch.sum(torch.cat(embeds,dim=1),dim=1).unsqueeze(1)
        #TODO: add bias
        tag_all_embed = torch.tanh(tag_all_embed)
        dec_h0 = torch.tanh(self.tag_to_dec(tag_all_embed) + self.z_to_dec(z))
        dec_h0 = torch.permute(dec_h0, (1,0,2))
        if mode == 'train':
            recon_loss, recon_acc = self.recon_loss(reinflect_surf, z, dec_h0, tag_embeddings, tag_attention_masks, recon_type='sum')
        else:
            recon_loss, recon_acc = self.recon_loss_test(reinflect_surf, z, dec_h0, tag_embeddings, tag_attention_masks, recon_type='sum')
           
        # (batchsize)
        kl_loss = self.kl_loss(mu,logvar)
        
        log_py_prior = self.log_py_prior(case,polar,mood,evid,pos,per,num,tense,aspect,inter,poss)

        # (batchsize)
        recon_loss = recon_loss.squeeze(1)#.mean()
        #loss = log_py_prior + xloss + recon_loss + kl_weight * kl_loss
        loss = xloss + recon_loss + kl_weight * kl_loss

        return loss, xloss, tag_correct, tag_total, recon_loss, kl_loss, recon_acc

    def kl_loss(self, mu, logvar):
        # KL: (batch_size), mu: (batch_size, nz), logvar: (batch_size, nz)
        KL = 0.5 * (mu.pow(2) + logvar.exp() - logvar - 1).sum(dim=1)
        return KL

    def recon_loss(self, y, z, decoder_hidden, tag_attention_values, tag_attention_masks, recon_type='avg'):
        #remove end symbol
        src = y[:, :-1]
        # remove start symbol
        tgt = y[:, 1:]        
        batch_size, seq_len = src.size()
        n_sample = z.size(1)

        output_logits = []
        for di in range(seq_len):
            decoder_input = src[:,di].unsqueeze(1)  # Teacher forcing
            decoder_output, decoder_hidden, decoder_attention = self.decoder(
                decoder_input, z, decoder_hidden, tag_attention_values, tag_attention_masks)
            output_logits.append(decoder_output)
        
        # (batchsize, seq_len, vocabsize)
        output_logits = torch.cat(output_logits,dim=1)

        _tgt = tgt.contiguous().view(-1)
        
        # (batch_size  * seq_len, vocab_size)
        _output_logits = output_logits.view(-1, output_logits.size(2))

        # (batch_size * seq_len)
        recon_loss = self.decoder.loss(_output_logits,  _tgt)
        # (batch_size, seq_len)
        recon_loss = recon_loss.view(batch_size, n_sample, -1)

        # (batch_size, 1)
        if recon_type=='avg':
            # avg over tokens
            recon_loss = recon_loss.mean(-1)
        elif recon_type=='sum':
            # sum over tokens
            recon_loss = recon_loss.sum(-1)
        elif recon_type == 'eos':
            # only eos token
            recon_loss = recon_loss[:,:,-1]

        # avg over batches and samples
        recon_acc  = self.accuracy(output_logits, tgt)
        return recon_loss, recon_acc

    def recon_loss_test(self, y, z, decoder_hidden, tag_attention_values, tag_attention_masks, recon_type='avg'):
        #remove end symbol
        src = y[:, :-1]
        # remove start symbol
        tgt = y[:, 1:]        
        batch_size, seq_len = src.size()
        n_sample = z.size(1)

        decoder_input = src[:,0].unsqueeze(1)
        output_logits = []
        for di in range(seq_len):
            decoder_output, decoder_hidden, decoder_attention = self.decoder(
                decoder_input, z, decoder_hidden, tag_attention_values, tag_attention_masks)
            output_logits.append(decoder_output)
            topv, topi = decoder_output.topk(1)
            decoder_input = topi.squeeze(1).detach()  # detach from history as input
        # (batchsize, seq_len, vocabsize)
        output_logits = torch.cat(output_logits,dim=1)

        _tgt = tgt.contiguous().view(-1)
        
        # (batch_size  * seq_len, vocab_size)
        _output_logits = output_logits.view(-1, output_logits.size(2))

        # (batch_size * seq_len)
        recon_loss = self.decoder.loss(_output_logits,  _tgt)
        # (batch_size, seq_len)
        recon_loss = recon_loss.view(batch_size, n_sample, -1)

        # (batch_size, 1)
        if recon_type=='avg':
            # avg over tokens
            recon_loss = recon_loss.mean(-1)
        elif recon_type=='sum':
            # sum over tokens
            recon_loss = recon_loss.sum(-1)
        elif recon_type == 'eos':
            # only eos token
            recon_loss = recon_loss[:,:,-1]

        # avg over batches and samples
        recon_acc, recon_preds  = self.accuracy(output_logits, tgt, mode='val')
        return recon_loss, recon_acc, recon_preds

    def log_py_prior(self, case,polar,mood,evid,pos,per,num,tense,aspect,inter,poss):
        batch_size,_ = case.size()
        tags = [case,polar,mood,evid,pos,per,num,tense,aspect,inter,poss]
        sft = nn.Softmax(dim=1)
        loss = nn.CrossEntropyLoss(reduce=False,ignore_index=0)
        logpy = torch.tensor(0.0).to('cuda')
        for i in range(len(tags)):
            prior = self.priors[i]
            # (batchsize, vocabsize of that tag)
            prior = sft(prior.repeat(batch_size,1))
            #to = F.one_hot(tags[i]).squeeze(1).float()
            #exclude pad hots
            #to =to * (tags[i] != 0)
            logpy+=loss(prior.to('cuda'),tags[i].squeeze(1)).mean()
        return logpy

    def log_py_prior_w_gumbels(self, gumbel_logits):
        # gumbel_tag_embeddings: (batchsize, 11, tag_emb_dim)
        batch_size = gumbel_logits[0].shape[0]
        sft = nn.Softmax(dim=1)
        loss = nn.CrossEntropyLoss(reduce=False)
        logpy = torch.tensor(0.0).to('cuda')
        for i in range(len(gumbel_logits)):
            prior = self.priors[i]
            # (batchsize, vocabsize of that tag)
            prior = sft(prior.repeat(batch_size,1))
            logpy+=loss(prior.to('cuda'), gumbel_logits[i]).mean()
        return logpy

    def generate(self, x, case,polar,mood,evid,pos,per,num,tense,aspect,inter,poss):
        # a * [U(x)] + [Lu (xs|xt)] + [Ll (xt, yt| xs) - D(xt|yt)]

         # Ll (xt, yt | xs)
        mu, logvar, encoder_fhs = self.encoder(x)
        # (batchsize, 1, nz)
        z = mu.unsqueeze(0)

        tags = [case,polar,mood,evid,pos,per,num,tense,aspect,inter,poss]
        embeds =[]
        tag_embeds = []
        tag_attention_masks = []

        for i in range(len(tags)):
            tag_attention_masks.append(tags[i]==0)
            tag_mask = (tags[i]!=0).unsqueeze(1).repeat(1,1,self.tag_embed_dim)
            tag_emb = self.tag_embeddings[i](tags[i])
            tag_embeds.append(tag_emb)
            embed = tag_mask * tag_emb #+ self.tag_embeddings_biases[i])
            embeds.append(embed)


        # (batchsize, 11, tag_emb_dim)
        tag_embeddings = torch.cat(tag_embeds,dim=1)
        # (batchsize, 1, 11)
        tag_attention_masks = torch.permute(torch.stack(tag_attention_masks), (1,2,0))
    

        # (batchsize, 1, tag_emb_dim)
        tag_all_embed = torch.sum(torch.cat(embeds,dim=1),dim=1).unsqueeze(1)
        #TODO: add bias
        tag_all_embed = torch.tanh(tag_all_embed)
        decoder_hidden = torch.tanh(self.tag_to_dec(tag_all_embed) + self.z_to_dec(z))
     
        #### GREEDY DECODING
        '''decoder_input = torch.tensor(1).unsqueeze(0).unsqueeze(0).to('cuda')
        output_logits = []
        preds = []
        di = 0
        while True:
            decoder_output, decoder_hidden, decoder_attention = self.decoder(
                decoder_input, z, decoder_hidden, tag_attention_values)
            output_logits.append(decoder_output)
            topv, topi = decoder_output.topk(1)
            decoder_input = topi.squeeze(1).detach()  # detach from history as input
            char = self.decoder.vocab.id2word(decoder_input.item())
            preds.append(char)
            di +=1
            if di==20 or char == '</s>':
                break
        reinflected_form = ''.join(preds)
        return reinflected_form'''

      
        ### BEAM SEARCH DECODING
        K = 8
        decoded_batch = []
       
        # decoding goes sentence by sentence
        for idx in range(1):
            # Start with the start of the sentence token
            decoder_input = torch.tensor([[self.decoder.vocab["<s>"]]], dtype=torch.long, device='cuda')
            decoder_hidden = decoder_hidden[:,idx,:].unsqueeze(1)

            node = BeamSearchNode(decoder_hidden, None, decoder_input, 0., 1)
            live_hypotheses = [node]

            completed_hypotheses = []

            t = 0
            while len(completed_hypotheses) < K and t < 100:
                t += 1
                # (len(live), 1)
                decoder_input = torch.cat([node.wordid for node in live_hypotheses], dim=0)
                # (1, len(live), nh)
                decoder_hidden_h = torch.cat([node.h for node in live_hypotheses], dim=1)
                decoder_hidden = decoder_hidden_h

                #(len(live), 1, nz)
                expanded_z = z[idx].view(1, 1, -1).expand(len(live_hypotheses), 1, self.nz)
                expanded_tag_embeddings = tag_embeddings[idx].view(1, 11, -1).expand(len(live_hypotheses), 11, 200)
                expanded_tag_attention_masks = tag_attention_masks[idx].view(1, 1, -1).expand(len(live_hypotheses), 1, 11)
                output_logits, decoder_hidden, _ = self.decoder(decoder_input, expanded_z, decoder_hidden, expanded_tag_embeddings, expanded_tag_attention_masks)
                decoder_output = F.log_softmax(output_logits, dim=-1)

                prev_logp = torch.tensor([node.logp for node in live_hypotheses], dtype=torch.float, device='cuda')
                decoder_output = decoder_output + prev_logp.view(len(live_hypotheses), 1, 1)

                # (len(live) * vocab_size)
                decoder_output = decoder_output.view(-1)

                # (K)
                log_prob, indexes = torch.topk(decoder_output, K-len(completed_hypotheses))

                live_ids = indexes // len(self.decoder.vocab)
                word_ids = indexes % len(self.decoder.vocab)

                live_hypotheses_new = []
                for live_id, word_id, log_prob_ in zip(live_ids, word_ids, log_prob):
                    node = BeamSearchNode(decoder_hidden[:, live_id, :].unsqueeze(1),
                        live_hypotheses[live_id], word_id.view(1, 1), log_prob_, t)

                    if word_id.item() == self.decoder.vocab["</s>"]:
                        completed_hypotheses.append(node)
                    else:
                        live_hypotheses_new.append(node)

                live_hypotheses = live_hypotheses_new

                if len(completed_hypotheses) == K:
                    break

            for live in live_hypotheses:
                completed_hypotheses.append(live)

            utterances = []
            for n in sorted(completed_hypotheses, key=lambda node: node.logp, reverse=True):
                utterance = []
                utterance.append(self.decoder.vocab.id2word(n.wordid.item()))
                # back trace
                while n.prevNode != None:
                    n = n.prevNode
                    utterance.append(self.decoder.vocab.id2word(n.wordid.item()))

                utterance = utterance[::-1]
                utterances.append(utterance)
                # only save the top 1
                break
            decoded_batch.append(utterances[0])

        return ''.join(decoded_batch[0])

    def reparameterize(self, mu, logvar, nsamples=1):
        batch_size, nz = mu.size()
        std = logvar.mul(0.5).exp()
        mu_expd = mu.unsqueeze(1).expand(batch_size, nsamples, nz)
        std_expd = std.unsqueeze(1).expand(batch_size, nsamples, nz)
        eps = torch.zeros_like(std_expd).normal_()
        return mu_expd + torch.mul(eps, std_expd)
    
    def accuracy(self, output_logits, tgt, mode='train'):
        # calculate correct number of predictions 
        batch_size, T = tgt.size()
        sft = nn.Softmax(dim=2)
        # (batchsize, T)
        pred_tokens = torch.argmax(sft(output_logits),2)
        acc = (pred_tokens == tgt).sum().item()
        #print(''.join(self.decoder.vocab.decode_sentence(pred_tokens[0])))
        if mode == 'val':
            return acc, pred_tokens
        else:
            return acc


class BeamSearchNode(object):
    def __init__(self, hiddenstate, previousNode, wordId, logProb, length):
        '''
        :param hiddenstate:
        :param previousNode:
        :param wordId:
        :param logProb:
        :param length:
        '''
        self.h = hiddenstate
        self.prevNode = previousNode
        self.wordid = wordId
        self.logp = logProb
        self.leng = length

    def eval(self, alpha=1.0):
        reward = 0
        # Add here a function for shaping a reward
        return self.logp / float(self.leng - 1 + 1e-6) + alpha * reward



        