#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time    : 17-9-22 下午1:39
# @Author  : Luo Yao
# @Site    : http://github.com/TJCVRS
# @File    : train_shadownet.py
# @IDE: PyCharm Community Edition
"""
Train shadow net script
"""
import os
import tensorflow as tf
import os.path as ops
import time
import numpy as np
import argparse

from models.crnn import crnn_model

from utils import data_utils, log_utils
from utils.config_utils import load_config
from utils.log_utils import compute_accuracy

logger = log_utils.init_logger()


def init_args():
    """
    :return:
    """
    parser = argparse.ArgumentParser()
    parser.add_argument('-d', '--dataset_dir', type=str, help='Path to dir containing train/test data and annotation files.')
    parser.add_argument('-w', '--weights_path', type=str, help='Path to pre-trained weights.')
    parser.add_argument('-j', '--num_threads', type=int, default=int(os.cpu_count()/2), help='Number of threads to use in batch shuffling')
    parser.add_argument('-e', '--decode', default=False, help='Activate decoding of predictions during training (slow!)')
    return parser.parse_args()


def train_shadownet(dataset_dir, weights_path=None, decode: bool=False, num_threads=4):
    """
    :param dataset_dir:
    :param weights_path:
    :param num_threads: Number of threads to use in tf.train.shuffle_batch
    :return:
    """
    # Load config
    cfg = load_config().cfg

    # decode the tf records to get the training data
    decoder = data_utils.TextFeatureIO().reader
    input_images, input_labels, input_image_names = decoder.read_features(ops.join(dataset_dir, 'train_feature.tfrecords'), cfg.TRAIN.BATCH_SIZE, num_threads)

    # initialise the net model
    shadownet = crnn_model.ShadowNet(phase='Train', hidden_nums=cfg.ARCH.HIDDEN_UNITS, layers_nums=cfg.ARCH.HIDDEN_LAYERS, num_classes=len(decoder.char_dict) + 1)

    with tf.variable_scope('shadow', reuse=False):
        net_out = shadownet.build_shadownet(inputdata=input_images)

    cost = tf.reduce_mean(tf.nn.ctc_loss(labels=input_labels, inputs=net_out,
                                         sequence_length=cfg.ARCH.SEQ_LENGTH*np.ones(cfg.TRAIN.BATCH_SIZE)))

    decoded, log_prob = tf.nn.ctc_beam_search_decoder(net_out,
                                                      cfg.ARCH.SEQ_LENGTH*np.ones(cfg.TRAIN.BATCH_SIZE),
                                                      merge_repeated=False)

    sequence_dist = tf.reduce_mean(tf.edit_distance(tf.cast(decoded[0], tf.int32), input_labels))

    global_step = tf.Variable(0, name='global_step', trainable=False)

    starter_learning_rate = cfg.TRAIN.LEARNING_RATE
    learning_rate = tf.train.exponential_decay(starter_learning_rate, global_step,
                                               cfg.TRAIN.LR_DECAY_STEPS, cfg.TRAIN.LR_DECAY_RATE,
                                               staircase=True)
    update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)

    with tf.control_dependencies(update_ops):
        optimizer = tf.train.AdadeltaOptimizer(learning_rate=learning_rate).minimize(loss=cost, global_step=global_step)

    # Setup TF summary
    tboard_save_path = 'tboard/shadownet'
    if not ops.exists(tboard_save_path):
        os.makedirs(tboard_save_path)
    tf.summary.scalar(name='Cost', tensor=cost)
    tf.summary.scalar(name='Learning_Rate', tensor=learning_rate)
    if decode:
        tf.summary.scalar(name='Seq_Dist', tensor=sequence_dist)

    merge_summary_op = tf.summary.merge_all()

    # Set saver configuration
    saver = tf.train.Saver()
    model_save_dir = cfg.PATH.CRNN_MODEL_SAVE_DIR
    if not ops.exists(model_save_dir):
        os.makedirs(model_save_dir)
    train_start_time = time.strftime('%Y-%m-%d-%H-%M-%S', time.localtime(time.time()))
    model_name = 'shadownet_{:s}.ckpt'.format(str(train_start_time))
    model_save_path = ops.join(model_save_dir, model_name)

    # Set sess configuration
    sess_config = tf.ConfigProto()
    sess_config.gpu_options.per_process_gpu_memory_fraction = cfg.TRAIN.GPU_MEMORY_FRACTION
    sess_config.gpu_options.allow_growth = cfg.TRAIN.TF_ALLOW_GROWTH

    sess = tf.Session(config=sess_config)

    summary_writer = tf.summary.FileWriter(tboard_save_path)
    summary_writer.add_graph(sess.graph)

    # Set the training parameters
    train_epochs = cfg.TRAIN.EPOCHS

    with sess.as_default():
        if weights_path is None:
            logger.info('Training from scratch')
            init = tf.global_variables_initializer()
            sess.run(init)
        else:
            logger.info('Restore model from {:s}'.format(weights_path))
            saver.restore(sess=sess, save_path=weights_path)

        cost_history = [np.inf]
        for epoch in range(train_epochs):
            if decode:
                _, c, seq_distance, predictions, labels, summary = sess.run([optimizer, cost, sequence_dist, decoded, input_labels, merge_summary_op])

                labels = decoder.sparse_tensor_to_str(labels)
                predictions = decoder.sparse_tensor_to_str(predictions[0])
                accuracy = compute_accuracy(labels, predictions)

                if epoch % cfg.TRAIN.DISPLAY_STEP == 0:
                    logger.info('Epoch: {:d} cost= {:9f} seq distance= {:9f} train accuracy= {:9f}'.format(
                        epoch + 1, c, seq_distance, accuracy))

            else:
                _, c, summary = sess.run([optimizer, cost, merge_summary_op])
                if epoch % cfg.TRAIN.DISPLAY_STEP == 0:
                    logger.info('Epoch: {:d} cost= {:9f}'.format(epoch + 1, c))

            cost_history.append(c)
            summary_writer.add_summary(summary=summary, global_step=epoch)
            saver.save(sess=sess, save_path=model_save_path, global_step=epoch)

        return np.array(cost_history[1:])  # Don't return the first np.inf


if __name__ == '__main__':
    # init args
    args = init_args()

    if not ops.exists(args.dataset_dir):
        raise ValueError('{:s} doesn\'t exist'.format(args.dataset_dir))

    train_shadownet(args.dataset_dir, args.weights_path, args.decode, args.num_threads)
    print('Done')