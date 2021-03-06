#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import itertools
import os

import sys
from datetime import datetime


COMMAND = '''PYTHONPATH=. anaconda-python3-gpu -m project.models.{model} -F {args}  '''


def cartesian_product(dicts):
    return list(dict(zip(dicts, x)) for x in itertools.product(*dicts.values()))


def to_name(configuration):
    kvs = sorted([(k, v)
                  for k, v in configuration.items()], key=lambda e: e[0])
    name = configuration.get("name", "auto_")
    return name + '-'.join([('{}_{}'.format(k[:1], v)) for (k, v) in kvs if k not in ["logdir"]])


def to_cmd(model, **kwargs):
    arg_list = " ".join(["--{}={}".format(k.replace('_', '-'), v)
                         for k, v in kwargs.items()])
    return COMMAND.format(model=model, args=arg_list)


def main(_):
    now = datetime.strftime(datetime.now(), '%d%m_%H%M%S')

    model = 'code2vec_solo'
    log_path = '/home/ehambro/EWEEZ/project/logs'
    qstat_logs = "/home/ehambro/EWEEZ/project/qstat_logs/{}".format(now)

    hyperparameters_space = dict(
        char_seq=[60],
        vocab_size=[40000],
        #char_embed=[70],
        desc_embed=[200],
        batch_size=[128],
        # CODE2VEC ONLY
        path_embed=[300],
        path_vocab=[15000],
        path_seq=[5000],
        code2vec_size=[300],

        lstm_size=[300],
        bidirectional=[1],

        no_dup=[10],
        epochs=[100],
        dropout=[0.0, 0.1, 0.3],
        tokenizer=['var_only'],
        name=['c2vsolo_sweep'],
        code_tokenizer=['code2vec'],
        save_every=[3],
        logdir=[log_path]
    )

    configurations = cartesian_product(hyperparameters_space)

    # Check that we are on the UCLCS cluster first
    if os.path.exists('/home/ehambro/'):
        # If the folder that will contain logs does not exist, create it
        if not os.path.exists(log_path):
            os.makedirs(log_path)
        if not os.path.exists(qstat_logs):
            os.makedirs(qstat_logs)

    configurations = list(configurations)

    command_lines = set()
    for cfg in configurations:
        name = to_name(cfg)

        command_line = '{} --name={} >> {}/{}.log 2>&1'.format(
            to_cmd(model, **cfg), name, qstat_logs, name)
        command_lines |= {command_line}

    # Sort command lines and remove duplicates
    sorted_command_lines = sorted(command_lines)
    nb_jobs = len(sorted_command_lines)

    header = """#!/bin/bash

#$ -cwd
#$ -S /bin/bash
#$ -o /dev/null
#$ -e /dev/null
#$ -t 1-{}
#$ -l tmem=10G
#$ -l h_rt=12:00:00
#$ -P gpu
#$ -l gpu=1

export LANG="en_US.utf8"
export LANGUAGE="en_US:en"

cd /home/ehambro/EWEEZ/project/
export PYTHONPATH=.

""".format(nb_jobs)

    print(header)

    for job_id, command_line in enumerate(sorted_command_lines, 1):
        print('test $SGE_TASK_ID -eq {} && {}\n'.format(job_id, command_line))


if __name__ == '__main__':
    main(sys.argv[1:])
