# Copyright (c) 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
#

import os
import sys
import time
import argparse

import numpy as np

import torch
from torch.autograd import Variable
import torch.nn as nn

from data import get_nli_dev_test, get_batch, build_vocab
from mutils import get_optimizer
from models import NLINet


parser = argparse.ArgumentParser(description='NLI training')
# paths
parser.add_argument("--nlipath", type=str, default='dataset/SNLI/', help="NLI data path (SNLI or MultiNLI)")
parser.add_argument("--outputdir", type=str, default='savedir/', help="Output directory")
parser.add_argument("--outputmodelname", type=str, default='model.pickle')
parser.add_argument("--word_emb_path", type=str, default="dataset/GloVe/glove.840B.300d.txt", help="word embedding file path")

# training
parser.add_argument("--n_epochs", type=int, default=20)
parser.add_argument("--batch_size", type=int, default=64)
parser.add_argument("--dpout_model", type=float, default=0., help="encoder dropout")
parser.add_argument("--dpout_fc", type=float, default=0., help="classifier dropout")
parser.add_argument("--nonlinear_fc", type=float, default=0, help="use nonlinearity in fc")
parser.add_argument("--optimizer", type=str, default="sgd,lr=0.1", help="adam or sgd,lr=0.1")
parser.add_argument("--lrshrink", type=float, default=5, help="shrink factor for sgd")
parser.add_argument("--decay", type=float, default=0.99, help="lr decay")
parser.add_argument("--minlr", type=float, default=1e-5, help="minimum lr")
parser.add_argument("--max_norm", type=float, default=5., help="max norm (grad clipping)")

# model
parser.add_argument("--encoder_type", type=str, default='InferSent', help="see list of encoders") # JP: V1 removed
parser.add_argument("--enc_lstm_dim", type=int, default=2048, help="encoder nhid dimension")
parser.add_argument("--n_enc_layers", type=int, default=1, help="encoder num layers")
parser.add_argument("--fc_dim", type=int, default=512, help="nhid of fc layers")
parser.add_argument("--n_classes", type=int, default=3, help="entailment/neutral/contradiction")
parser.add_argument("--pool_type", type=str, default='max', help="max or mean")

# gpu
parser.add_argument("--gpu_id", type=int, default=0, help="GPU ID")
parser.add_argument("--seed", type=int, default=1234, help="seed")

# data
parser.add_argument("--word_emb_dim", type=int, default=300, help="word embedding dimension")

params, _ = parser.parse_known_args()

# set gpu device
torch.cuda.set_device(params.gpu_id)

# print parameters passed, and all parameters
print('\ntogrep : {0}\n'.format(sys.argv[1:]))
print(params)


"""
SEED
"""
np.random.seed(params.seed)
torch.manual_seed(params.seed)
torch.cuda.manual_seed(params.seed)

"""
DATA
"""
valid, test = get_nli_dev_test(params.nlipath)
word_vec = build_vocab(valid['s1'] + valid['s2'] +
                       test['s1'] + test['s2'], params.word_emb_path)

for split in ['s1', 's2']:
    for data_type in ['valid', 'test']:
        eval(data_type)[split] = np.array([['<s>'] +
            [word for word in sent.split() if word in word_vec] +
            ['</s>'] for sent in eval(data_type)[split]])


"""
MODEL
"""
# model config
config_nli_model = {
    'n_words'        :  len(word_vec)          ,
    'word_emb_dim'   :  params.word_emb_dim   ,
    'enc_lstm_dim'   :  params.enc_lstm_dim   ,
    'n_enc_layers'   :  params.n_enc_layers   ,
    'dpout_model'    :  params.dpout_model    ,
    'dpout_fc'       :  params.dpout_fc       ,
    'fc_dim'         :  params.fc_dim         ,
    'bsize'          :  params.batch_size     ,
    'n_classes'      :  params.n_classes      ,
    'pool_type'      :  params.pool_type      ,
    'nonlinear_fc'   :  params.nonlinear_fc   ,
    'encoder_type'   :  params.encoder_type   ,
    'use_cuda'       :  True                  ,

}

# model
encoder_types = ['InferSent', 'BLSTMprojEncoder', 'BGRUlastEncoder',
                 'InnerAttentionMILAEncoder', 'InnerAttentionYANGEncoder',
                 'InnerAttentionNAACLEncoder', 'ConvNetEncoder', 'LSTMEncoder']
assert params.encoder_type in encoder_types, "encoder_type must be in " + \
                                             str(encoder_types)


MODEL_PATH = 'savedir_from_scratch/model.pickle'
nli_net = NLINet(config_nli_model)
nli_net.load_state_dict(torch.load(MODEL_PATH))
print(nli_net)

# loss
weight = torch.FloatTensor(params.n_classes).fill_(1)
loss_fn = nn.CrossEntropyLoss(weight=weight)
loss_fn.size_average = False

# optimizer
optim_fn, optim_params = get_optimizer(params.optimizer)
optimizer = optim_fn(nli_net.parameters(), **optim_params)

# cuda by default
nli_net.cuda()
loss_fn.cuda()


"""
TRAIN
"""
val_acc_best = -1e10
adam_stop = False
stop_training = False
lr = optim_params['lr'] if 'sgd' in params.optimizer else None


def evaluate(epoch, eval_type='valid', final_eval=False):
    nli_net.eval()
    correct = 0.
    global val_acc_best, lr, stop_training, adam_stop

    if eval_type == 'valid':
        print('\nVALIDATION : Epoch {0}'.format(epoch))

    s1 = valid['s1'] if eval_type == 'valid' else test['s1']
    s2 = valid['s2'] if eval_type == 'valid' else test['s2']
    target = valid['label'] if eval_type == 'valid' else test['label']

    for i in range(0, len(s1), params.batch_size):
        # prepare batch
        s1_batch, s1_len = get_batch(s1[i:i + params.batch_size], word_vec, params.word_emb_dim)
        s2_batch, s2_len = get_batch(s2[i:i + params.batch_size], word_vec, params.word_emb_dim)
        s1_batch, s2_batch = Variable(s1_batch.cuda()), Variable(s2_batch.cuda())
        tgt_batch = Variable(torch.LongTensor(target[i:i + params.batch_size])).cuda()

        # model forward
        output = nli_net((s1_batch, s1_len), (s2_batch, s2_len))

        pred = output.data.max(1)[1]
        correct += pred.long().eq(tgt_batch.data.long()).cpu().numpy().sum() #numpy

    # save model
    eval_acc = round(100 * correct / len(s1), 2)
    if final_eval:
        print('finalgrep : accuracy {0} : {1}'.format(eval_type, eval_acc))
    else:
        print('togrep : results : epoch {0} ; mean accuracy {1} :\
              {2}'.format(epoch, eval_type, eval_acc))

    if eval_type == 'valid' and epoch <= params.n_epochs:
        if eval_acc > val_acc_best:
            print('saving model at epoch {0}'.format(epoch))
            if not os.path.exists(params.outputdir):
                os.makedirs(params.outputdir)
            torch.save(nli_net.state_dict(), os.path.join(params.outputdir,
                       params.outputmodelname))
            val_acc_best = eval_acc
        else:
            if 'sgd' in params.optimizer:
                optimizer.param_groups[0]['lr'] = optimizer.param_groups[0]['lr'] / params.lrshrink
                print('Shrinking lr by : {0}. New lr = {1}'
                      .format(params.lrshrink,
                              optimizer.param_groups[0]['lr']))
                if optimizer.param_groups[0]['lr'] < params.minlr:
                    stop_training = True
            if 'adam' in params.optimizer:
                # early stopping (at 2nd decrease in accuracy)
                stop_training = adam_stop
                adam_stop = True
    return eval_acc


"""
Train model on Natural Language Inference task
"""
epoch = 1

# Run best model on test set.

print('\nTEST : Epoch {0}'.format(epoch))
evaluate(1e6, 'valid', True)
evaluate(0, 'test', True)

# Save encoder instead of full model
#torch.save(nli_net.encoder.state_dict(), os.path.join(params.outputdir, params.outputmodelname + '.encoder.pkl'))
