# --------------------------------------------------------
# Tensorflow iCAN Bare
# Licensed under The MIT License [see LICENSE for details]
# Written by Wentao Ma
# --------------------------------------------------------

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf
import tensorflow.contrib.slim as slim
from tensorflow.contrib.slim import arg_scope
from tensorflow.contrib.slim.python.slim.nets import resnet_utils
from tensorflow.contrib.slim.python.slim.nets import resnet_v1
from tensorflow.contrib.layers.python.layers import layers
from tensorflow.contrib.layers.python.layers import regularizers
from tensorflow.python.ops import nn_ops
from tensorflow.contrib.layers.python.layers import initializers
from tensorflow.python.framework import ops

from ult.config import cfg
from ult.visualization import draw_bounding_boxes_HOI

import numpy as np
import ipdb


def resnet_arg_scope(is_training=True,
                     weight_decay=cfg.TRAIN.WEIGHT_DECAY,
                     batch_norm_decay=0.997,
                     batch_norm_epsilon=1e-5,
                     batch_norm_scale=True):
    batch_norm_params = {
        'is_training': False,
        'decay': batch_norm_decay,
        'epsilon': batch_norm_epsilon,
        'scale': batch_norm_scale,
        'trainable': False,
        'updates_collections': ops.GraphKeys.UPDATE_OPS
    }
    with arg_scope(
            [slim.conv2d, slim.fully_connected],
            weights_regularizer=tf.contrib.layers.l2_regularizer(cfg.TRAIN.WEIGHT_DECAY),
            weights_initializer=slim.variance_scaling_initializer(),
            biases_regularizer=tf.contrib.layers.l2_regularizer(cfg.TRAIN.WEIGHT_DECAY),
            biases_initializer=tf.constant_initializer(0.0),
            trainable=is_training,
            activation_fn=tf.nn.relu,
            normalizer_fn=slim.batch_norm,
            normalizer_params=batch_norm_params):
        with arg_scope([slim.batch_norm], **batch_norm_params) as arg_sc:
            return arg_sc


class ResNet50():
    def __init__(self):
        self.visualize = {}
        self.intermediate = {}
        self.predictions = {}
        self.score_summaries = {}
        self.event_summaries = {}
        self.train_summaries = []
        self.losses = {}

        self.image = tf.placeholder(tf.float32, shape=[1, None, None, 3], name='image')
        self.spatial = tf.placeholder(tf.float32, shape=[None, 64, 64, 2], name='sp')
        self.Hsp_boxes = tf.placeholder(tf.float32, shape=[None, 5], name='Hsp_boxes')
        self.O_boxes = tf.placeholder(tf.float32, shape=[None, 5], name='O_boxes')
        self.gt_class_H = tf.placeholder(tf.float32, shape=[None, 29], name='gt_class_H')
        self.gt_class_HO = tf.placeholder(tf.float32, shape=[None, 29], name='gt_class_HO')
        self.gt_class_sp = tf.placeholder(tf.float32, shape=[None, 29], name='gt_class_sp')
        self.Mask_HO = tf.placeholder(tf.float32, shape=[None, 29], name='HO_mask')
        self.Mask_H = tf.placeholder(tf.float32, shape=[None, 29], name='H_mask')
        self.Mask_sp = tf.placeholder(tf.float32, shape=[None, 29], name='sp_mask')
        self.H_num = tf.placeholder(tf.int32)
        self.num_classes = 29
        self.num_fc = 1024
        self.scope = 'resnet_v1_50'
        self.stride = [16, ]
        self.lr = tf.placeholder(tf.float32)
        if tf.__version__ == '1.1.0':
            self.blocks = [resnet_utils.Block('block1', resnet_v1.bottleneck, [(256, 64, 1)] * 2 + [(256, 64, 2)]),
                           resnet_utils.Block('block2', resnet_v1.bottleneck, [(512, 128, 1)] * 3 + [(512, 128, 2)]),
                           resnet_utils.Block('block3', resnet_v1.bottleneck, [(1024, 256, 1)] * 5 + [(1024, 256, 1)]),
                           resnet_utils.Block('block4', resnet_v1.bottleneck, [(2048, 512, 1)] * 3),
                           resnet_utils.Block('block5', resnet_v1.bottleneck, [(2048, 512, 1)] * 3)]
        else:
            from tensorflow.contrib.slim.python.slim.nets.resnet_v1 import resnet_v1_block
            self.blocks = [resnet_v1_block('block1', base_depth=64, num_units=3, stride=2),
                           resnet_v1_block('block2', base_depth=128, num_units=4, stride=2),
                           resnet_v1_block('block3', base_depth=256, num_units=6, stride=1),
                           resnet_v1_block('block4', base_depth=512, num_units=3, stride=1),
                           resnet_v1_block('block5', base_depth=512, num_units=3, stride=1)]

    def build_base(self):
        with tf.variable_scope(self.scope, self.scope):
            net = resnet_utils.conv2d_same(self.image, 64, 7, stride=2, scope='conv1')
            net = tf.pad(net, [[0, 0], [1, 1], [1, 1], [0, 0]])
            net = slim.max_pool2d(net, [3, 3], stride=2, padding='VALID', scope='pool1')

        return net

    def image_to_head(self, is_training):
        with slim.arg_scope(resnet_arg_scope(is_training=False)):
            net = self.build_base()
            net, _ = resnet_v1.resnet_v1(net,
                                         self.blocks[0:cfg.RESNET.FIXED_BLOCKS],
                                         global_pool=False,
                                         include_root_block=False,
                                         scope=self.scope)
        with slim.arg_scope(resnet_arg_scope(is_training=is_training)):
            head, _ = resnet_v1.resnet_v1(net,
                                          self.blocks[cfg.RESNET.FIXED_BLOCKS:-2],  # - (Restore_flag - 3)
                                          global_pool=False,
                                          include_root_block=False,
                                          scope=self.scope)
        return head

    def res5(self, pool5_H, pool5_O, is_training, name):
        with slim.arg_scope(resnet_arg_scope(is_training=is_training)):
            fc7_H, _ = resnet_v1.resnet_v1(pool5_H,
                                           self.blocks[-2:-1],
                                           global_pool=False,
                                           include_root_block=False,
                                           reuse=False,
                                           scope=self.scope)

            fc7_H = tf.reduce_mean(fc7_H, axis=[1, 2])

            fc7_O, _ = resnet_v1.resnet_v1(pool5_O,
                                           self.blocks[-1:],
                                           global_pool=False,
                                           include_root_block=False,
                                           reuse=False,
                                           scope=self.scope)

            fc7_O = tf.reduce_mean(fc7_O, axis=[1, 2])

        return fc7_H, fc7_O

    def head_to_tail(self, fc7_H, fc7_O, is_training, name):
        with slim.arg_scope(resnet_arg_scope(is_training=is_training)):
            fc8_SH = slim.fully_connected(fc7_H[:self.H_num, :], self.num_fc, scope='fc8_SH')
            fc8_SH = slim.dropout(fc8_SH, keep_prob=0.5, is_training=is_training, scope='dropout8_SH')
            fc9_SH = slim.fully_connected(fc8_SH, self.num_fc, scope='fc9_SH')
            fc9_SH = slim.dropout(fc9_SH, keep_prob=0.5, is_training=is_training, scope='dropout9_SH')

            fc8_SO = slim.fully_connected(fc7_O, self.num_fc, scope='fc8_SO')
            fc8_SO = slim.dropout(fc8_SO, keep_prob=0.5, is_training=is_training, scope='dropout8_SO')
            fc9_SO = slim.fully_connected(fc8_SO, self.num_fc, scope='fc9_SO')
            fc9_SO = slim.dropout(fc9_SO, keep_prob=0.5, is_training=is_training, scope='dropout9_SO')

        return fc9_SH, fc9_SO

    def crop_pool_layer(self, bottom, rois, name):
        with tf.variable_scope(name) as scope:

            batch_ids = tf.squeeze(tf.slice(rois, [0, 0], [-1, 1], name="batch_id"), [1])
            bottom_shape = tf.shape(bottom)
            height = (tf.to_float(bottom_shape[1]) - 1.) * np.float32(self.stride[0])
            width = (tf.to_float(bottom_shape[2]) - 1.) * np.float32(self.stride[0])
            x1 = tf.slice(rois, [0, 1], [-1, 1], name="x1") / width
            y1 = tf.slice(rois, [0, 2], [-1, 1], name="y1") / height
            x2 = tf.slice(rois, [0, 3], [-1, 1], name="x2") / width
            y2 = tf.slice(rois, [0, 4], [-1, 1], name="y2") / height

            bboxes = tf.stop_gradient(tf.concat([y1, x1, y2, x2], axis=1))
            if cfg.RESNET.MAX_POOL:
                pre_pool_size = cfg.POOLING_SIZE * 2
                crops = tf.image.crop_and_resize(bottom, bboxes, tf.to_int32(batch_ids), [pre_pool_size, pre_pool_size],
                                                 name="crops")
                crops = slim.max_pool2d(crops, [2, 2], padding='SAME')
            else:
                crops = tf.image.crop_and_resize(bottom, bboxes, tf.to_int32(batch_ids),
                                                 [cfg.POOLING_SIZE, cfg.POOLING_SIZE], name="crops")
        return crops

    def region_classification(self, fc7_H, fc7_O, is_training, initializer, name):
        with tf.variable_scope(name) as scope:
            cls_score_H = slim.fully_connected(fc7_H, self.num_classes,
                                               weights_initializer=initializer,
                                               trainable=is_training,
                                               activation_fn=None, scope='cls_score_H')
            cls_prob_H = tf.nn.sigmoid(cls_score_H, name='cls_prob_H')
            tf.reshape(cls_prob_H, [1, self.num_classes])

            cls_score_O = slim.fully_connected(fc7_O, self.num_classes,
                                               weights_initializer=initializer,
                                               trainable=is_training,
                                               activation_fn=None, scope='cls_score_O')
            cls_prob_O = tf.nn.sigmoid(cls_score_O, name='cls_prob_O')
            tf.reshape(cls_prob_O, [1, self.num_classes])

            self.predictions["cls_score_H"] = cls_score_H
            self.predictions["cls_prob_H"] = cls_prob_H
            self.predictions["cls_score_O"] = cls_score_O
            self.predictions["cls_prob_O"] = cls_prob_O

            self.predictions["cls_prob_HO_final"] = cls_prob_O + cls_prob_H

        return cls_prob_H, cls_prob_O

    def build_network(self, is_training):
        initializer = tf.random_normal_initializer(mean=0.0, stddev=0.01)

        # ResNet Backbone
        head = self.image_to_head(is_training)
        pool5_H = self.crop_pool_layer(head, self.Hsp_boxes, 'Crop_H')
        pool5_O = self.crop_pool_layer(head, self.O_boxes, 'Crop_O')

        fc7_H, fc7_O = self.res5(pool5_H, pool5_O, is_training, 'res5')

        fc7_SH, fc7_SO = self.head_to_tail(fc7_H, fc7_O, is_training, 'fc_HO')

        cls_prob_H, cls_prob_O = self.region_classification(fc7_SH, fc7_SO, is_training,
                                                                         initializer, 'classification')

        self.score_summaries.update(self.predictions)

        return cls_prob_H, cls_prob_O

    def create_architecture(self, is_training):

        cls_prob_H, cls_prob_O = self.build_network(is_training)

        for var in tf.trainable_variables():
            self.train_summaries.append(var)

        self.add_loss()
        layers_to_output = {}
        layers_to_output.update(self.losses)

        val_summaries = []
        with tf.device("/cpu:0"):
            val_summaries.append(self.add_gt_image_summary_H())
            val_summaries.append(self.add_gt_image_summary_HO())
            for key, var in self.event_summaries.items():
                val_summaries.append(tf.summary.scalar(key, var))

        val_summaries.append(tf.summary.scalar('lr', self.lr))
        self.summary_op = tf.summary.merge_all()
        self.summary_op_val = tf.summary.merge(val_summaries)

        return layers_to_output

    def add_loss(self):

        with tf.variable_scope('LOSS') as scope:
            cls_score_H = self.predictions["cls_score_H"]
            cls_score_O = self.predictions["cls_score_O"]

            label_H = self.gt_class_H
            label_HO = self.gt_class_HO

            H_mask = self.Mask_H
            HO_mask = self.Mask_HO

            H_cross_entropy = tf.reduce_mean(
                tf.multiply(tf.nn.sigmoid_cross_entropy_with_logits(labels=label_H, logits=cls_score_H), H_mask))
            HO_cross_entropy = tf.reduce_mean(
                tf.multiply(tf.nn.sigmoid_cross_entropy_with_logits(labels=label_HO, logits=cls_score_O), HO_mask))

            self.losses['H_cross_entropy'] = H_cross_entropy
            self.losses['HO_cross_entropy'] = HO_cross_entropy

            loss = 2 * H_cross_entropy + HO_cross_entropy

            self.losses['total_loss'] = loss
            self.event_summaries.update(self.losses)

        return loss

    def add_gt_image_summary_H(self):

        image = tf.py_func(draw_bounding_boxes_HOI,
                           [tf.reverse(self.image + cfg.PIXEL_MEANS, axis=[-1]), self.Hsp_boxes, self.gt_class_H],
                           tf.float32, name="gt_boxes_H")
        return tf.summary.image('GROUND_TRUTH_H', image)

    def add_gt_image_summary_HO(self):

        image = tf.py_func(draw_bounding_boxes_HOI,
                           [tf.reverse(self.image + cfg.PIXEL_MEANS, axis=[-1]), self.O_boxes, self.gt_class_HO],
                           tf.float32, name="gt_boxes_HO")
        return tf.summary.image('GROUND_TRUTH_HO)', image)

    def add_score_summary(self, key, tensor):
        tf.summary.histogram('SCORE/' + tensor.op.name + '/' + key + '/scores', tensor)

    def add_train_summary(self, var):
        tf.summary.histogram('TRAIN/' + var.op.name, var)

    def train_step(self, sess, blobs, lr, train_op):
        feed_dict = {self.image: blobs['image'], self.Hsp_boxes: blobs['Hsp_boxes'],
                     self.O_boxes: blobs['O_boxes'], self.gt_class_H: blobs['gt_class_H'],
                     self.gt_class_HO: blobs['gt_class_HO'], self.Mask_H: blobs['Mask_H'],
                     self.Mask_HO: blobs['Mask_HO'], self.spatial: blobs['sp'],
                     self.lr: lr, self.Mask_sp: blobs['Mask_sp'],
                     self.gt_class_sp: blobs['gt_class_sp'], self.H_num: blobs['H_num']}

        loss_cls_H, loss_cls_HO, loss, _ = sess.run([self.losses['H_cross_entropy'],
                                                     self.losses['HO_cross_entropy'],
                                                     self.losses['total_loss'],
                                                     train_op],
                                                    feed_dict=feed_dict)
        return loss_cls_H, loss_cls_HO, loss

    def train_step_with_summary(self, sess, blobs, lr, train_op):
        feed_dict = {self.image: blobs['image'], self.Hsp_boxes: blobs['Hsp_boxes'],
                     self.O_boxes: blobs['O_boxes'], self.gt_class_H: blobs['gt_class_H'],
                     self.gt_class_HO: blobs['gt_class_HO'], self.Mask_H: blobs['Mask_H'],
                     self.Mask_HO: blobs['Mask_HO'], self.spatial: blobs['sp'],
                     self.lr: lr, self.Mask_sp: blobs['Mask_sp'],
                     self.gt_class_sp: blobs['gt_class_sp'], self.H_num: blobs['H_num']}

        loss_cls_H, loss_cls_HO, loss, summary, _ = sess.run([self.losses['H_cross_entropy'],
                                                              self.losses['HO_cross_entropy'],
                                                              self.losses['total_loss'],
                                                              self.summary_op,
                                                              train_op],
                                                             feed_dict=feed_dict)
        return loss_cls_H, loss_cls_HO, loss, summary

    def test_image_H(self, sess, image, blobs):
        feed_dict = {self.image: image, self.Hsp_boxes: blobs['H_boxes'], self.H_num: blobs['H_num']}

        cls_prob_H = sess.run([self.predictions["cls_prob_H"]], feed_dict=feed_dict)

        return cls_prob_H

    def test_image_HO(self, sess, image, blobs):
        feed_dict = {self.image: image, self.Hsp_boxes: blobs['H_boxes'], self.O_boxes: blobs['O_boxes'],
                     self.spatial: blobs['sp'], self.H_num: blobs['H_num']}

        cls_prob_HO = sess.run([self.predictions["cls_prob_HO_final"]], feed_dict=feed_dict)

        return cls_prob_HO
