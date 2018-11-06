import os
import sys
import time
from collections import OrderedDict

import numpy

import tensorflow as tf
tf.logging.set_verbosity(tf.logging.INFO)

from .pointnet import pointnet
from larcv import larcv_interface



class trainercore(object):
    '''
    This class is the core interface for training.  Each function to
    be overridden for a particular interface is marked and raises
    a NotImplemented error.

    '''
    def __init__(self, config):
        self._config          = config
        self._larcv_interface = larcv_interface.larcv_interface()
        self._iteration       = 0
        self._outputs         = None

        self._core_training_params = [
            'SAVE_ITERATION',
            'LOGDIR',
            'ITERATIONS',
            'IO',
            'NETWORK'
        ]


        # Make sure that 'BASE_LEARNING_RATE' and 'TRAINING'
        # are in net network parameters:




        self._initialize(config)

    def _initialize(self, config):

        if 'MODE' not in config:
            config['MODE'] == "GPU"

        config['NETWORK']['TRAINING'] = self._config['TRAINING']
        config['NETWORK']['MODE'] = self._config['MODE']


        self._net = pointnet()
        self._net.set_params(config['NETWORK'])

    def check_params(self):
        for param in self._core_training_params:
            if param not in self._config:
                raise Exception("Missing paragmeter "+ str(param))
        return True


    # def delete(self):
    #     for key, manager in self._dataloaders.iteritems():
    # #         manager.stop_manager()


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


    def _construct_graph(self):

        # Net construction:
        start = time.time()
        sys.stdout.write("Begin constructing network\n")

        # Make sure all required dimensions are present:

        dims = self._larcv_interface.fetch_minibatch_dims('TRAIN')

        # Call the function to define the inputs
        self._input   = self._initialize_input(dims)

        # Call the function to define the output
        self._logits, self._transformations  = self._net._build_network(self._input)


        # Apply a softmax and argmax:
        self._outputs = self._create_softmax(self._logits)

        self._accuracy = self._calculate_accuracy(self._input, self._outputs)

        # Create the loss function
        self._loss    = self._calculate_loss(self._input, self._logits, self._transformations)



        end = time.time()
        sys.stdout.write("Done constructing network. ({0:.2}s)\n".format(end-start))


    def initialize(self):

        # Verify the network object is set:
        if not hasattr(self, '_net'):
            raise Exception("Must set network object by calling set_network_object() before initialize")

        self._initialize_io()

        self._construct_graph()

        # Create an optimizer:
        if self._config['BASE_LEARNING_RATE'] <= 0:
            opt = tf.train.AdamOptimizer()
        else:
            opt = tf.train.AdamOptimizer(self._config['BASE_LEARNING_RATE'])

        self._global_step = tf.train.get_or_create_global_step()
        self._train_op = opt.minimize(self._loss, self._global_step)


        hooks = self.get_standard_hooks()

        config = tf.ConfigProto()

        if self._config['MODE'] == "CPU":
            config.inter_op_parallelism_threads = 2
            config.intra_op_parallelism_threads = 128
        if self._config['MODE'] == "GPU":
            config.gpu_options.allow_growth = True

        # self._sess = tf.train.MonitoredTrainingSession(config=config)
        self._sess = tf.train.MonitoredTrainingSession(config=config, hooks = hooks)


    def get_standard_hooks(self):

        loss_is_nan_hook = tf.train.NanTensorHook(
            self._loss,
            fail_on_nan_loss=True,
        )

        # Create a hook to manage the checkpoint saving:
        save_checkpoint_hook = tf.train.CheckpointSaverHook(
            checkpoint_dir = self._config["LOGDIR"],
            save_steps=self._config["SAVE_ITERATION"]
            )

        # Create a hook to manage the summary saving:
        summary_saver_hook = tf.train.SummarySaverHook(
            save_steps = self._config['SUMMARY_ITERATION'],
            output_dir = self._config['LOGDIR'],
            summary_op = tf.summary.merge_all()
            )


        # Create a profiling hook for tracing:
        profile_hook = tf.train.ProfilerHook(
            save_steps    = self._config['PROFILE_ITERATION'],
            output_dir    = self._config['LOGDIR'],
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
            loss_is_nan_hook,
            save_checkpoint_hook,
            summary_saver_hook,
            profile_hook,
            logging_hook,
        ]

        return hooks

    def _initialize_input(self, dims):
        '''Initialize input parameters of the network.  Must return a dict type

        For exampe, paremeters of the dict can be 'image', 'label', 'weights', etc

        Arguments:
            dims {[type]} -- [description]

        Keyword Arguments:
            label_dims {[type]} -- [description] (default: {None})

        Raises:
            NotImplementedError -- [description]
        '''

        inputs = dict()

        batch_size = self._config['MINIBATCH_SIZE']

        inputs.update({
            'image' : tf.placeholder(tf.float32, [batch_size, None, 3]),
            'label' :  tf.placeholder(tf.int64,   dims['label'], name="input_label"),
            }
        )

        # inputs.update({
        #     'image' :  tf.placeholder(tf.float32, dims['image'], name="input_image"),
        #     'label' :  tf.placeholder(tf.int64,   dims['label'], name="input_label"),
        # })




        return inputs


    def _create_softmax(self, logits):
        '''Must return a dict type

        [description]

        Arguments:
            logits {[type]} -- [description]

        Raises:
            NotImplementedError -- [description]
        '''

        # For the logits, we compute the softmax and the predicted label


        output = dict()

        # Take the logits (which are one per plane) and create a softmax and prediction (one per plane)

        output['softmax'] = tf.nn.softmax(logits)
        output['prediction'] = tf.argmax(logits, axis=-1)


        return output



    # def compute_weights(self, labels, boost_labels = None):
    #     '''
    #     This is NOT a tensorflow implementation, but a numpy implementation.
    #     Running on CPUs this might not make a difference.  Running on GPUs
    #     it might be good to move this to a GPU, but I suspect it's not needed.
    #     '''
    #     # Take the labels, and compute the per-label weight


    #     # Prepare output weights:
    #     weights = numpy.zeros(labels.shape)

    #     i = 0
    #     for batch in labels:
    #         # First, figure out what the labels are and how many of each:
    #         values, counts = numpy.unique(batch, return_counts=True)

    #         n_pixels = numpy.sum(counts)
    #         for value, count in zip(values, counts):
    #             weight = 1.0*(n_pixels - count) / n_pixels
    #             if boost_labels is not None and value in boost_labels.keys():
    #                 weight *= boost_labels[value]
    #             mask = labels[i] == value
    #             weights[i, mask] += weight
    #         weights[i] *= 1. / numpy.sum(weights[i])
    #         i += 1



    #     # Normalize the weights to sum to 1 for each event:
    #     return weights



    def _calculate_loss(self, inputs, logits, transformations):
        ''' Calculate the loss.

        returns a single scalar for the optimizer to use.
        '''


        with tf.name_scope('cross_entropy'):

            # Traditional Classification loss:            
            loss = tf.reduce_mean(
                tf.nn.softmax_cross_entropy_with_logits_v2(labels=inputs['label'],
                                                           logits=logits)
                )
            tf.summary.scalar("Classification_Loss",loss)

            # If desired, add weight regularization loss:
            if 'REGULARIZE' in self._config['NETWORK']:
                reg_loss = tf.reduce_mean(tf.losses.get_regularization_losses())
                tf.summary.scalar("Weight_Regularization", reg_loss)
                loss += reg_loss

            # Add a regularization loss against the matrix transformations:
            t_loss = None
            for transformation in transformations:
                mat_dim = transformation.get_shape().as_list()[-1]
                difference_to_identity = tf.eye(mat_dim, batch_shape = [self._config['MINIBATCH_SIZE']]) 
                difference_to_identity -= tf.matmul(transformation, tf.matrix_transpose(transformation))
                this_t_loss = tf.reduce_mean(tf.nn.l2_normalize(difference_to_identity))
                this_t_loss = self._config['TRANSFORMATION_LOSS']*this_t_loss
                if t_loss is None:
                    t_loss = this_t_loss
                else:
                    t_loss += this_t_loss

            if t_loss is not None:
                tf.summary.scalar("Transformation_Loss", t_loss)
                loss += t_loss

            # Total summary:
            tf.summary.scalar("Total_Loss",loss)

            return loss




    def _calculate_accuracy(self, inputs, outputs):
        ''' Calculate the accuracy.  Computes total average accuracy,
        accuracy on just non zero pixels, and accuracy on just neutrino pixels.

        '''

        # Compare how often the input label and the output prediction agree:


        with tf.name_scope('accuracy'):

            correct_prediction = tf.equal(tf.argmax(inputs['label'], -1),
                                          outputs['prediction'])
            accuracy = tf.reduce_mean(tf.cast(correct_prediction, tf.float32))
            tf.summary.scalar("Accuracy", accuracy)

        return accuracy




    def feed_dict(self, inputs):
        '''Build the feed dict

        Take input images, labels and match
        to the correct feed dict tensorrs

        This is probably overridden in the subclass, but here you see the idea

        Arguments:
            images {dict} -- Dictionary containing the input tensors

        Returns:
            [dict] -- Feed dictionary for a tf session run call

        '''
        fd = dict()

        for key in inputs:
            if inputs[key] is not None:
                fd.update({self._input[key] : inputs[key]})

        return fd


    def image_to_point_cloud(self, image):

        # This function maps an image to a point cloud


        # Have to do this image by image to get the right shape:
        outputs = []
        max_points= 0
        for b in range(image.shape[0]):

            non_zero_locs = list(numpy.where(image[b] != 0))
            # Calculate the values and put them last:
            values = image[b][tuple(non_zero_locs)]
            if values.shape[0] > max_points:
                max_points = values.shape[0]
            # Merge everything into an N by 2 array:
            non_zero_locs.append(values)
            point_dim_len = len(non_zero_locs)
            # Stack everythin together and take the transpose:
            res = numpy.stack((non_zero_locs)).T
            outputs.append(res)

        # Stack the results into a uniform numpy array padded with zeros:
        output = numpy.zeros([len(outputs), max_points, point_dim_len])
        for i, o in enumerate(outputs):
            npoints = len(o)
            output[i,0:npoints,:] = o

        return output



    def train_step(self):


        minibatch_data = self._larcv_interface.fetch_minibatch_data('TRAIN')
        minibatch_dims = self._larcv_interface.fetch_minibatch_dims('TRAIN')


        for key in minibatch_data:
            minibatch_data[key] = numpy.reshape(minibatch_data[key], minibatch_dims[key])

        # Reduce the image's extra dimensions:
        minibatch_data['image'] = numpy.squeeze(minibatch_data['image'])
        minibatch_data['image'] = self.image_to_point_cloud(minibatch_data['image'])



        self._sess.run(self._train_op, 
                       feed_dict = self.feed_dict(inputs = minibatch_data))


    def batch_process(self):

        # Run iterations
        for i in range(self._config['ITERATIONS']):
            if self._config['TRAINING'] and self._iteration >= self._config['ITERATIONS']:
                print('Finished training (iteration %d)' % self._iteration)
                break

            # Start IO thread for the next batch while we train the network
            self.train_step()
