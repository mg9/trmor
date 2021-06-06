from itertools import chain
import math
import torch
import torch.nn as nn

from torch.nn.utils.rnn import pad_packed_sequence, pack_padded_sequence
from encoder import GaussianEncoderBase

def log_sum_exp(value, dim=None, keepdim=False):
    """Numerically stable implementation of the operation
    value.exp().sum(dim, keepdim).log()
    """
    if dim is not None:
        m, _ = torch.max(value, dim=dim, keepdim=True)
        value0 = value - m
        if keepdim is False:
            m = m.squeeze(dim)
        return m + torch.log(torch.sum(torch.exp(value0), dim=dim, keepdim=keepdim))
    else:
        m = torch.max(value)
        sum_exp = torch.sum(torch.exp(value - m))
        return m + torch.log(sum_exp)

    
class LSTMEncoder(GaussianEncoderBase):
    """Gaussian LSTM Encoder with constant-length batching"""
    def __init__(self, args, vocab_size, model_init, emb_init):
        super(LSTMEncoder, self).__init__()
        self.ni = args.ni
        self.nh = args.enc_nh
        self.nz = args.nz

        self.embed = nn.Embedding(vocab_size, args.ni)
        self.lstm = nn.LSTM(input_size=args.ni,
                            hidden_size=args.enc_nh,
                            num_layers=1,
                            batch_first=True,
                            dropout=0)

        # dimension transformation to z (mean and logvar)
        self.linear = nn.Linear(args.enc_nh, 2 * args.nz, bias=False)
        self.reset_parameters(model_init, emb_init)

    def reset_parameters(self, model_init, emb_init):
        # for name, param in self.lstm.named_parameters():
        #     # self.initializer(param)
        #     if 'bias' in name:
        #         nn.init.constant_(param, 0.0)
        #         # model_init(param)
        #     elif 'weight' in name:
        #         model_init(param)

        # model_init(self.linear.weight)
        # emb_init(self.embed.weight)
        for param in self.parameters():
            model_init(param)
        emb_init(self.embed.weight)


    def forward(self, input):
        """
        Args:
            x: (batch_size, seq_len)

        Returns: Tensor1, Tensor2
            Tensor1: the mean tensor, shape (batch, nz)
            Tensor2: the logvar tensor, shape (batch, nz)
        """

        # (batch_size, seq_len-1, args.ni)
        word_embed = self.embed(input)
        _, (last_state, last_cell) = self.lstm(word_embed)
        mean, logvar = self.linear(last_state).chunk(2, -1)
        return mean.squeeze(0), logvar.squeeze(0)

    # def eval_inference_mode(self, x):
    #     """compute the mode points in the inference distribution
    #     (in Gaussian case)
    #     Returns: Tensor
    #         Tensor: the posterior mode points with shape (*, nz)
    #     """

    #     # (batch_size, nz)
    #     mu, logvar = self.forward(x)


class VarLSTMEncoder(LSTMEncoder):
    """Gaussian LSTM Encoder with variable-length batching"""
    def __init__(self, args, vocab_size, model_init, emb_init):
        super(VarLSTMEncoder, self).__init__(args, vocab_size, model_init, emb_init)


    def forward(self, input):
        """
        Args:
            input: tuple which contains x and sents_len
                    x: (batch_size, seq_len)
                    sents_len: long tensor of sentence lengths

        Returns: Tensor1, Tensor2
            Tensor1: the mean tensor, shape (batch, nz)
            Tensor2: the logvar tensor, shape (batch, nz)
        """

        input, sents_len = input
        # (batch_size, seq_len, args.ni)
        word_embed = self.embed(input)
        packed_embed = pack_padded_sequence(word_embed, sents_len.tolist(), batch_first=True)
        _, (last_state, last_cell) = self.lstm(packed_embed)
        mean, logvar = self.linear(last_state).chunk(2, -1)
        return mean.squeeze(0), logvar.squeeze(0)

    def encode(self, input, nsamples):
        """perform the encoding and compute the KL term
        Args:
            input: tuple which contains x and sents_len

        Returns: Tensor1, Tensor2
            Tensor1: the tensor latent z with shape [batch, nsamples, nz]
            Tensor2: the tenor of KL for each x with shape [batch]

        """
        # (batch_size, nz)
        mu, logvar = self.forward(input)
        # (batch, nsamples, nz)
        z = self.reparameterize(mu, logvar, nsamples)
        KL = 0.5 * (mu.pow(2) + logvar.exp() - logvar - 1).sum(dim=1)
        return z, KL



