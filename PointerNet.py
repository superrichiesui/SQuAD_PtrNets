#!/usr/bin/env python3

import torch
import torch.nn as nn
from torch.nn import Parameter
import torch.nn.functional as F
from torch import tanh, sigmoid




from pytorch_pretrained_bert import BertTokenizer, BertModel, BertForMaskedLM
modelBERT = BertModel.from_pretrained('bert-base-uncased')
USE_CUDA = torch.cuda.is_available()
if USE_CUDA:
    modelBERT.cuda()
#modelBERT.eval()
tokenizer = BertTokenizer.from_pretrained('bert-base-uncased')


def getContextBertEmbeddings(sentence):
    sentences = "[CLS]" + sentence + "[SEP]"
    tokenized_text = tokenizer.tokenize(sentence)
    if len(tokenized_text) > 511:
        tokenized_text = tokenized_text[:511]
    
    indexed_token = tokenizer.convert_tokens_to_ids(tokenized_text)
    batch_i = 0
    #print (indexed_token)
    # Convert inputs to PyTorch tensors
    segments_ids = [1] * len(tokenized_text)
    tokens_tensor = torch.tensor([indexed_token])

    segments_tensors = torch.tensor([segments_ids])

    if USE_CUDA:
        tokens_tensor = tokens_tensor.cuda()
        segments_tensors = segments_tensors.cuda()

    #print (segments_tensors, tokens_tensor, len(tokenized_text))
    with torch.no_grad():
        encoded_layers, _ = modelBERT(tokens_tensor, segments_tensors)
    token_embeddings = [] 

# For each token in the sentence...
    for token_i in range(len(tokenized_text)):
      # Holds 12 layers of hidden states for each token 
      hidden_layers = [] 
      # For each of the 12 layers...
      for layer_i in range(len(encoded_layers)):
        # Lookup the vector for `token_i` in `layer_i`
        vec = encoded_layers[layer_i][batch_i][token_i]
        hidden_layers.append(vec)
      token_embeddings.append(hidden_layers)

    # Sanity check the dimensions:
    # print ("Number of tokens in sequence:", len(token_embeddings))
    # print ("Number of layers per token:", len(token_embeddings[0]))

    concatenated_last_4_layers = [torch.cat((layer[-1], layer[-2], layer[-3], layer[-4]), 0) for layer in token_embeddings] # [number_of_tokens, 3072]
    summed_last_4_layers = [torch.sum(torch.stack(layer)[-4:], 0) for layer in token_embeddings] # [number_of_tokens, 768]
    #print (torch.stack(summed_last_4_layers).shape)
    return torch.stack(summed_last_4_layers)



class Encoder(nn.Module):
    """
    Encoder class for Pointer-Net
    """

    def __init__(self, embedding_dim,
                 hidden_dim,
                 n_layers,
                 dropout,
                 bidir):
        """
        Initiate Encoder

        :param Tensor embedding_dim: Number of embbeding channels
        :param int hidden_dim: Number of hidden units for the LSTM
        :param int n_layers: Number of layers for LSTMs
        :param float dropout: Float between 0-1
        :param bool bidir: Bidirectional
        """

        super(Encoder, self).__init__()
        self.hidden_dim = hidden_dim//2 if bidir else hidden_dim
        self.n_layers = n_layers*2 if bidir else n_layers
        self.bidir = bidir
        self.lstm = nn.LSTM(embedding_dim,
                            self.hidden_dim,
                            n_layers,
                            dropout=dropout,
                            bidirectional=bidir)

        # Used for propagating .cuda() command
        self.h0 = Parameter(torch.zeros(1), requires_grad=False)
        self.c0 = Parameter(torch.zeros(1), requires_grad=False)

    def forward(self, embedded_inputs,
                hidden):
        """
        Encoder - Forward-pass

        :param Tensor embedded_inputs: Embedded inputs of Pointer-Net
        :param Tensor hidden: Initiated hidden units for the LSTMs (h, c)
        :return: LSTMs outputs and hidden units (h, c)
        """

        embedded_inputs = embedded_inputs.permute(1, 0, 2)

        outputs, hidden = self.lstm(embedded_inputs, hidden)

        return outputs.permute(1, 0, 2), hidden


class Attention(nn.Module):
    """
    Attention model for Pointer-Net
    """

    def __init__(self, input_dim,
                 hidden_dim):
        """
        Initiate Attention

        :param int input_dim: Input's diamention
        :param int hidden_dim: Number of hidden units in the attention
        """

        super(Attention, self).__init__()

        self.input_dim = input_dim
        self.hidden_dim = hidden_dim

        self.input_linear = nn.Linear(input_dim, hidden_dim)
        self.context_linear = nn.Conv1d(input_dim, hidden_dim, 1, 1)
        self.V = Parameter(torch.FloatTensor(hidden_dim), requires_grad=True)
        self._inf = Parameter(torch.FloatTensor([float('-inf')]), requires_grad=False)
        self.tanh = tanh
        self.softmax = nn.Softmax(dim=1)

        # Initialize vector V
        nn.init.uniform_(self.V, -1, 1)

    def forward(self, input,
                context,
                mask):
        """
        Attention - Forward-pass

        :param Tensor input: Hidden state h
        :param Tensor context: Attention context
        :param ByteTensor mask: Selection mask
        :return: tuple of - (Attentioned hidden state, Alphas)
        """

        # (batch, hidden_dim, seq_len)
        inp = self.input_linear(input).unsqueeze(2).expand(-1, -1, context.size(1))

        # (batch, hidden_dim, seq_len)
        context = context.permute(0, 2, 1)
        ctx = self.context_linear(context)

        # (batch, 1, hidden_dim)
        V = self.V.unsqueeze(0).expand(context.size(0), -1).unsqueeze(1)

        # (batch, seq_len)
        att = torch.bmm(V, self.tanh(inp + ctx)).squeeze(1)
        if len(att[mask]) > 0:
            att[mask] = self.inf[mask]
        alpha = self.softmax(att)
        hidden_state = torch.bmm(ctx, alpha.unsqueeze(2)).squeeze(2)

        return hidden_state, alpha

    def init_inf(self, mask_size):
        self.inf = self._inf.unsqueeze(1).expand(*mask_size)


class Decoder(nn.Module):
    """
    Decoder model for Pointer-Net
    """

    def __init__(self, input_dim,
                 hidden_dim):
        """
        Initiate Decoder

        :param int input_dim: input dimension in Pointer-Net
        :param int hidden_dim: Number of hidden units for the decoder's RNN
        """

        super(Decoder, self).__init__()
        self.input_dim = input_dim
        self.hidden_dim = hidden_dim

        self.input_to_hidden = nn.Linear(input_dim, 4 * hidden_dim)
        self.hidden_to_hidden = nn.Linear(hidden_dim, 4 * hidden_dim)
        self.hidden_out = nn.Linear(hidden_dim * 2, hidden_dim)
        self.att = Attention(hidden_dim, hidden_dim)

        # Used for propagating .cuda() command
        self.mask = Parameter(torch.ones(1), requires_grad=False)
        self.runner = Parameter(torch.zeros(1), requires_grad=False)

    def forward(self, embedded_inputs,
                decoder_input_init,
                hidden,
                context):
        """
        Decoder - Forward-pass

        :param Tensor embedded_inputs: Embedded inputs of questions
        :param Tensor decoder_input_init: First decoder's input
        :param Tensor hidden: First decoder's hidden states
        :param Tensor context: Encoder's outputs
        :return: (Output probabilities, Pointers indices), last hidden state
        """
        
        batch_size = embedded_inputs.size(0)
        input_length = embedded_inputs.size(1)

        # (batch, seq_len)
        mask = self.mask.repeat(input_length).unsqueeze(0).repeat(batch_size, 1)
        self.att.init_inf(mask.size())

        # Generating arang(input_length), broadcasted across batch_size
        runner = self.runner.repeat(input_length)
        for i in range(input_length):
            runner.data[i] = i
        runner = runner.unsqueeze(0).expand(batch_size, -1).long()

        outputs = []
        pointers = []

        def step(x, hidden):
            """
            Recurrence step function

            :param Tensor x: Input at time t
            :param tuple(Tensor, Tensor) hidden: Hidden states at time t-1
            :return: Hidden states at time t (h, c), Attention probabilities (Alpha)
            """

            # Regular LSTM
            h, c = hidden
            
            gates = self.input_to_hidden(x) + self.hidden_to_hidden(h)
            input, forget, cell, out = gates.chunk(4, 1)

            input = sigmoid(input)
            forget = sigmoid(forget)
            cell = tanh(cell)
            out = sigmoid(out)

            c_t = (forget * c) + (input * cell)
            h_t = out * tanh(c_t)

            # Attention section
            hidden_t, output = self.att(h_t, context, torch.eq(mask, 0))
            hidden_t = tanh(self.hidden_out(torch.cat((hidden_t, h_t), 1)))

            return hidden_t, c_t, output

        decoder_input = decoder_input_init
        # Recurrence loop
        for _ in range(2):
            h_t, c_t, outs = step(decoder_input, hidden)
            hidden = (h_t, c_t)

            # Masking selected inputs
            masked_outs = outs * mask

            # Get maximum probabilities and indices
            max_probs, indices = masked_outs.max(1)
            one_hot_pointers = (runner == indices.unsqueeze(1).expand(-1, outs.size()[1])).float()

            # Update mask to ignore seen indices
            mask  = mask * (1 - one_hot_pointers)

            # Get embedded inputs by max indices
            embedding_mask = one_hot_pointers.unsqueeze(2).expand(-1, -1, self.input_dim).byte()
            
            decoder_input = embedded_inputs[embedding_mask.data].view(batch_size, self.input_dim)

            outputs.append(outs.unsqueeze(0))
            pointers.append(indices.unsqueeze(1))

        outputs = torch.cat(outputs).permute(1, 0, 2)
        pointers = torch.cat(pointers, 1)
        
        return (outputs, pointers), hidden


class PointerNet(nn.Module):
    """
    Pointer-Net
    """

    def __init__(self, vocab_sz, embedding_dim,
                 hidden_dim,
                 lstm_layers,
                 dropout,
                 bidir=False):
        """
        Initiate Pointer-Net

        :param int embedding_dim: Number of embbeding channels
        :param int hidden_dim: Encoders hidden units
        :param int lstm_layers: Number of layers for LSTMs
        :param float dropout: Float between 0-1
        :param bool bidir: Bidirectional
        """

        super(PointerNet, self).__init__()
        self.embedding_dim = embedding_dim
        self.bidir = bidir
        # self.embedding = nn.Embedding(vocab_sz, embedding_dim)
        self.para_encoder = Encoder(embedding_dim,
                               hidden_dim,
                               lstm_layers,
                               dropout,
                               bidir)
        self.question_encoder = Encoder(embedding_dim, hidden_dim, lstm_layers, dropout, bidir)
        self.downsize_linear = nn.Linear(2*hidden_dim, hidden_dim)
        self.decoder = Decoder(hidden_dim, hidden_dim)

    def forward(self, inputs, questions, inputs_text, questions_text):
        """
        PointerNet - Forward-pass

        :param Tensor inputs: Input sequence (paragraph)
        :param Tensor questions: Questions sequence
        :return: Pointers probabilities and indices
        """

        batch_size = inputs.size(0)
        input_length = inputs.size(1)
        quest_length = questions.size(1)
        
        # decoder_input0 = self.decoder_input0.unsqueeze(0).expand(batch_size, -1)

        inputs = inputs.view(batch_size * input_length, -1)
        quest_inputs = questions.view(batch_size * quest_length, -1)
        
        # embedded_inputs = self.embedding(inputs).view(batch_size, input_length, -1)
        # quest_embedded_inputs = self.embedding(quest_inputs).view(batch_size, quest_length, -1)

        embedded_inputs = getContextBertEmbeddings(inputs_text[0]).unsqueeze(1)
        embedded_inputs = embedded_inputs.view(1,embedded_inputs.size(0),-1)
        quest_embedded_inputs = getContextBertEmbeddings(questions_text[0]).unsqueeze(1)
        quest_embedded_inputs = quest_embedded_inputs.view(1,quest_embedded_inputs.size(0),-1)
        
        # batch_sz * q_len * n_dirs*hidden_sz, n_dir*n_layers * batch_sz * hidden_sz 
        quest_encoder_outputs, quest_encoder_hidden = self.question_encoder(quest_embedded_inputs, None)
       
        # batch_sz * para_len * n_dirs*hidden_sz, n_dir*n_layers * batch_sz * hidden_sz 
        encoder_outputs, encoder_hidden = self.para_encoder(embedded_inputs,
                                                       quest_encoder_hidden)

        if self.bidir:
            decoder_hidden0 = (torch.cat([_ for _ in encoder_hidden[0][-2:]], dim=-1),
                               torch.cat([_ for _ in encoder_hidden[1][-2:]], dim=-1))
            # Not used currently
            quest_decoder_hidden0 = (torch.cat([_ for _ in quest_encoder_hidden[0][-2:]], dim=-1),
                               torch.cat([_ for _ in quest_encoder_hidden[1][-2:]], dim=-1))
        else:
            decoder_hidden0 = (torch.cat((encoder_hidden[0][-1],quest_encoder_hidden[0][-1]),dim=1),
                               torch.cat((encoder_hidden[1][-1],quest_encoder_hidden[1][-1]),dim=1))
            decoder_hidden0 = (self.downsize_linear(decoder_hidden0[0]), self.downsize_linear(decoder_hidden0[1]))
            # Not used currently
            quest_decoder_hidden0 = (quest_encoder_hidden[0][-1],
                                     quest_encoder_hidden[1][-1])
        
        # batch_sz * 1 * n_dirs*hidden_sz 
        quest_final_feats = quest_encoder_outputs[:,-1,:].unsqueeze(1)
        #quest_encoding = self.quest_linear(quest_encoding)

        # concat para hidden and ques hidden to pass as input to decoder

        # bs * para_len * n_dirs*hidden_sz
        quest_final_bc = torch.cat(encoder_outputs.size(1) * [quest_final_feats],dim=1)

        # bs * para_len * 2*n_dirs*hidden_sz
        concat_encoded_feats = torch.cat((encoder_outputs,quest_final_bc),dim=2)
        concat_encoded_feats = self.downsize_linear(concat_encoded_feats)

        # Use the last output of question encoding as decoder initial input
        (outputs, pointers), decoder_hidden = self.decoder.forward(concat_encoded_feats,
                                                           quest_encoder_outputs[:,-1,:],
                                                           decoder_hidden0,
                                                           encoder_outputs)

        return  outputs, pointers
