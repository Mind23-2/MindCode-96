# Copyright 2021 Huawei Technologies Co., Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""import"""
import logging
import os
import argparse
import numpy as np
import moxing as mox

import mindspore.common.dtype as mstype
from mindspore.train.serialization import load_param_into_net
from mindspore.context import ParallelMode
from mindspore.communication.management import init
from mindspore import Tensor, context, load_checkpoint, export
from mindspore.train.callback import LossMonitor, TimeMonitor
from mindspore.train.model import Model
from mindspore.nn.optim import AdamWeightDecay
import mindspore
import mindspore.nn as nn
from mindspore.dataset import MindDataset
from mindspore.train.callback import CheckpointConfig, ModelCheckpoint
from src.training.optimizer import CustomWarmUpLR
from src.data.data_set import DataSet
from src.common.register import RegisterSet
from src.models.roberta_one_sent_classification_en import RobertaOneSentClassificationEn
from src.config import sstcfg, semcfg, bertcfg

device_id = int(os.getenv('DEVICE_ID', "0"))
device_num = int(os.getenv('RANK_SIZE', "0"))

def str2bool(v):
    """
    str2bool
    """
    # because argparse does not support to parse "true, False" as python
    # boolean directly
    return v.lower() in ("true", "t", "1")

class ArgumentGroup():
    """ArgumentGroup"""

    def __init__(self, parser, title, des):
        self._group = parser.add_argument_group(title=title, description=des)

    def add_arg(self, name, dtype, default, helpinfo,
                positional_arg=False, **kwargs):
        """add_arg"""
        prefix = "" if positional_arg else "--"
        dtype = str2bool if dtype == bool else dtype
        self._group.add_argument(
            prefix + name,
            default=default,
            type=dtype,
            help=helpinfo + ' Default: %(default)s.',
            **kwargs)

def build_common_arguments():
    """build_common_arguments"""
    parser = argparse.ArgumentParser(__doc__)
    model_g = ArgumentGroup(parser, "model", "model configuration and paths.")
    model_g.add_arg("job", str, 'SST-2', "job to be trained (SST-2 or Sem-L)")
    model_g.add_arg("data_url", str, "data/", "data url")
    model_g.add_arg("train_url", str, "obs://yyp/output/", "output url")
    model_g.add_arg("ckpt", str, "roberta.ckpt", "ckpt url")
    model_g.add_arg("batch_size", int, 24, "batch_size")
    model_g.add_arg(
        "--file_name",
        dtype=str,
        default="/cache/senta.air",
        helpinfo="Senta output air name.")
    model_g.add_arg(
        "--file_format",
        dtype=str,
        choices=[
            "AIR",
            "ONNX",
            "MINDIR"],
        default="AIR",
        helpinfo="file format")

    return parser.parse_args()

def dataset_reader_from_params(params_dict):
    """
    :param params_dict:
    :return:
    """
    dataset_reader = DataSet(params_dict)
    dataset_reader.build()

    return dataset_reader


def model_from_params(params_dict, train_dataset, epoch_nums, is_training=True):
    """
    :param params_dict:
    :return:
    """
    opt_params = params_dict.get("optimization", None)
    num_train_examples = 0
    # compute warmup_steps
    if opt_params:
        batch_size_train = train_dataset.get_batch_size()
        max_train_steps = train_dataset.get_dataset_size() * epoch_nums
        num_train_examples = batch_size_train * max_train_steps

        warmup_steps = opt_params.get("warmup_steps", 0)

        if warmup_steps == 0:
            warmup_proportion = opt_params.get("warmup_proportion", 0.1)
            warmup_steps = int(max_train_steps * warmup_proportion)

        opt_params = {}
        opt_params["warmup_steps"] = warmup_steps
        opt_params["max_train_steps"] = max_train_steps
        opt_params["num_train_examples"] = num_train_examples

        # combine params dict
        params_dict["optimization"].update(opt_params)

    return RobertaOneSentClassificationEn(bertcfg, is_training)



def build_trainer(params_dict, dataset_reader, model_class, num_train_examples=0):
    """build trainer"""
    trainer_name = params_dict.get("type", "CustomTrainer")
    trainer_class = RegisterSet.trainer.__getitem__(trainer_name)
    params_dict["num_train_examples"] = num_train_examples
    trainer = trainer_class(
        params=params_dict,
        data_set_reader=dataset_reader,
        model_class=model_class)
    return trainer


def build_optimizer(para, params):
    opt_params = para.get("optimization", "")
    lr_schedule = CustomWarmUpLR(learning_rate=opt_params.get('learning_rate', 3e-5),
                                 warmup_steps=opt_params.get('warmup_steps', 1),
                                 max_train_steps=opt_params.get('max_train_steps', 1))
    return AdamWeightDecay(params, learning_rate=lr_schedule, eps=1e-6)


if __name__ == "__main__":
    context.set_context(
        mode=context.GRAPH_MODE,
        device_target="Ascend",
        device_id=device_id)

    if device_num > 1:
        init()
        context.set_auto_parallel_context(device_num=device_num,
                                          parallel_mode=ParallelMode.DATA_PARALLEL,
                                          gradients_mean=True)

    args = build_common_arguments()
    task_name = args.job
    param_dict = None
    if task_name == "SST-2":
        param_dict = sstcfg
    if task_name == "Sem-L":
        param_dict = semcfg
    epoch_num = 1 #param_dict['dataset_reader']['train_reader']['config']['epoch']
    MINDRECORD_FILE_PATH = os.path.join(args.data_url, task_name, task_name + "_train.mindrecord")


    fp = os.path.join('/cache/data/', str(device_id))
    mox.file.copy_parallel(os.path.join(args.data_url, task_name), fp)
    ds = MindDataset(os.path.join(fp, task_name + "_train.mindrecord"),
                     num_parallel_workers=8, shuffle=True, num_shards=
                     device_num, shard_id=device_id).batch(args.batch_size, True,
                                                           8)


    model_params_dict = param_dict.get("model", "")

    model_with_loss = model_from_params(model_params_dict, ds, epoch_num)

    mox.file.copy_parallel(args.ckpt, '/cache/data/roberta.ckpt')

    weights = load_checkpoint('/cache/data/roberta.ckpt')
    unloaded = load_param_into_net(model_with_loss, weights)
    print(unloaded)

    logging.info("Weights loaded")
    optimizer = build_optimizer(
        model_params_dict,
        model_with_loss.trainable_params())
    train_network = nn.TrainOneStepCell(model_with_loss, optimizer)
    train_network.set_train()
    model = Model(train_network)
    tm = TimeMonitor()
    lm = LossMonitor()
    ckpt_config = CheckpointConfig(save_checkpoint_steps=ds.get_dataset_size(),
                                   keep_checkpoint_max=10, saved_network=train_network.network)
    ckpoint_cb = ModelCheckpoint(prefix=task_name+'-'+str(device_id), directory=args.train_url, config=ckpt_config)
    model.train(epoch_num, ds, callbacks=[tm, lm, ckpoint_cb], dataset_sink_mode=True)

    try:
        mindspore.save_checkpoint(model_with_loss.bert, args.train_url+ '/model.ckpt')
    except IOError:
        pass


    net = model_with_loss
    net.set_train(False)

    src_ids = Tensor(
        np.zeros([args.batch_size, bertcfg.seq_length]), mstype.int32)
    sent_ids = Tensor(
        np.zeros([args.batch_size, bertcfg.seq_length]), mstype.int32)
    mask_ids = Tensor(
        np.zeros([args.batch_size, bertcfg.seq_length]), mstype.int32)

    input_data = [src_ids, sent_ids, mask_ids]

    export(
        net.bert,
        *input_data,
        file_name=args.file_name,
        file_format=args.file_format)


    try:
        logging.info("copy file")
        mox.file.copy_parallel('/cache/', args.train_url)
    except IOError:
        pass
