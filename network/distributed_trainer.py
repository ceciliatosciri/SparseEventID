import os
import sys
import time
from collections import OrderedDict

import numpy

import tensorflow as tf

import horovod.tensorflow as hvd
hvd.init()

from larcv.distributed_larcv_interface import larcv_interface

from .uresnet import uresnet
from .trainercore import trainercore


class distributed_trainer(trainercore):
    '''
    This class is the core interface for training.  Each function to
    be overridden for a particular interface is marked and raises
    a NotImplemented error.

    '''
    def __init__(self, config):
        # Rely on the base class for most standard parameters, only
        # search for parameters relevant for distributed computing here

        # Put the IO rank as the last rank in the COMM, since rank 0 does tf saves
        root_rank = hvd.size() - 1 

        self._config          = config
        self._larcv_interface = larcv_interface(root=root_rank)
        self._iteration       = 0
        self._outputs         = None

        self._core_training_params = [
            'SAVE_ITERATION',
            'LOGDIR',
            'ITERATIONS',
            'IO',
            'NETWORK',
            'INTER_OP_PARALLELISM_THREADS',
            'INTRA_OP_PARALLELISM_THREADS',
        ]


        # Make sure that 'BASE_LEARNING_RATE' and 'TRAINING'
        # are in net network parameters:

        self._initialize(config)



    def _initialize_io(self):

        # Prepare data managers:
        for mode in self._config['IO']:

            if mode not in ['TRAIN', 'TEST', 'ANA']:
                raise Exception("Unknown mode {} requested, must be in ['TRAIN', 'TEST', 'ANA']".format(mode))


            io_cfg = {
                'filler_cfg'
            }

            io_config = {
                'filler_name' : self._config['IO'][mode]['FILLER'],
                'filler_cfg'  : self._config['IO'][mode]['FILE'],
                'verbosity'   : self._config['IO'][mode]['VERBOSITY']
            }
            data_keys = OrderedDict({
                'image': self._config['IO'][mode]['KEYWORD_DATA'], 
                'label': self._config['IO'][mode]['KEYWORD_LABEL']
                })

            self._larcv_interface.prepare_manager(mode, io_config, self._config['MINIBATCH_SIZE'], data_keys)



    def initialize(self):

        tf.logging.info("HVD rank: {}".format(hvd.rank()))


        # Verify the network object is set:
        if not hasattr(self, '_net'):
            raise Exception("Must set network object by calling set_network_object() before initialize")


        self._initialize_io()

        self._construct_graph()

        # Create an optimizer:
        if self._config['BASE_LEARNING_RATE'] <= 0:
            opt = tf.train.AdagradOptimizer()
        else:
            opt = tf.train.AdagradOptimizer(self._config['BASE_LEARNING_RATE']*hvd.size())

        with tf.variable_scope("hvd"):
            opt = hvd.DistributedOptimizer(opt)
        
            self._global_step = tf.train.get_or_create_global_step()
            self._train_op = opt.minimize(self._loss, self._global_step)

        hooks = self.get_distributed_hooks()

        if self._config['MODE'] == "CPU":
            config.inter_op_parallelism_threads = 2
            config.intra_op_parallelism_threads = 128
        if self._config['MODE'] == "GPU":
            config.gpu_options.allow_growth = True
            config.gpu_options.visible_device_list = str(hvd.local_rank())


        config = tf.ConfigProto()
        config.inter_op_parallelism_threads = self._config['INTER_OP_PARALLELISM_THREADS']
        config.intra_op_parallelism_threads = self._config['INTRA_OP_PARALLELISM_THREADS']

        self._sess = tf.train.MonitoredTrainingSession(config=config, hooks = hooks)

    def get_distributed_hooks(self):

        if hvd.rank() == 0:

            checkpoint_dir = self._config['LOGDIR']

            loss_is_nan_hook = tf.train.NanTensorHook(
                self._loss,
                fail_on_nan_loss=True,
            )

            # Create a hook to manage the checkpoint saving:
            save_checkpoint_hook = tf.train.CheckpointSaverHook( 
                checkpoint_dir = checkpoint_dir,
                save_steps=self._config["SAVE_ITERATION"]
                )

            # Create a hook to manage the summary saving:
            summary_saver_hook = tf.train.SummarySaverHook(
                save_steps = self._config['SUMMARY_ITERATION'],
                output_dir = checkpoint_dir,
                summary_op = tf.summary.merge_all()
                )

            
            # Create a profiling hook for tracing:
            profile_hook = tf.train.ProfilerHook(
                save_steps    = self._config['PROFILE_ITERATION'],
                output_dir    = checkpoint_dir,
                show_dataflow = True,
                show_memory   = True
            )

            logging_hook = tf.train.LoggingTensorHook(
                tensors       = { 'global_step' : self._global_step,
                                  'accuracy'    : self._accuracy, 
                                  'loss'        : self._loss},
                every_n_iter  = self._config['LOGGING_ITERATION'],
                )

            hooks = [
                hvd.BroadcastGlobalVariablesHook(0),
                loss_is_nan_hook,
                save_checkpoint_hook,
                summary_saver_hook,
                profile_hook,
                logging_hook,
            ]

        else:
            hooks = [
                hvd.BroadcastGlobalVariablesHook(0),
            ]

        return hooks

    def train_step(self):


        minibatch_data = self._larcv_interface.fetch_minibatch_data('TRAIN')
        minibatch_dims = self._larcv_interface.fetch_minibatch_dims('TRAIN')

        # Reshape labels by dict entry, if needed, or all at once:
        minibatch_data['label'] = numpy.reshape(minibatch_data['label'], minibatch_dims['label'])


        # Compute weights for this minibatch:
        minibatch_data['weight'] = self.compute_weights(minibatch_data['label'])

        _, loss, logits = self._sess.run([self._train_op, self._loss, self._logits],
                       feed_dict = self.feed_dict(inputs = minibatch_data))


        # tf.logging.info("Rank {} loss: {}".format(hvd.rank(), loss))
        # tf.logging.info("Rank {} loss: {}".format(hvd.rank(), logits))



