import sys
sys.path.insert(0, "/search/odin/Nick/_python_build")
import re
import abc
import time
import shutil
import logging as log
from util import * 
from config import confs

from QueueReader import *
import random
import tensorflow as tf

from tensorflow.python.ops import variable_scope
from tensorflow.python import debug as tf_debug
from tensorflow.python.framework import ops

from tensorflow.python.saved_model import builder as saved_model_builder
from tensorflow.python.saved_model import constants
from tensorflow.python.saved_model import signature_constants 
from tensorflow.python.saved_model import loader
from tensorflow.python.saved_model import main_op
from tensorflow.python.saved_model import signature_def_utils
from tensorflow.python.saved_model import tag_constants
from tensorflow.python.saved_model import utils
from tensorflow.contrib.layers.python.layers.embedding_ops import embedding_lookup_unique
from tensorflow.contrib.tensorboard.plugins import projector

from nick_tf import score_decoder 
from nick_tf import helper, helper1_2
from nick_tf import basic_decoder, basic_decoder1_2
from nick_tf import decoder, decoder1_2
from nick_tf import beam_decoder
from nick_tf import dynamic_attention_wrapper, attention_wrapper1_2
from nick_tf.cocob_optimizer import COCOB 

import hook 

graphlg = log.getLogger("graph")
trainlg = log.getLogger("train")

class ModelCore(object):
	def __init__(self, name, job_type="single", task_id="0", dtype=tf.float32):
		self.conf = confs[name]
		self.model_kind = self.__class__.__name__
		if self.conf.model_kind !=  self.model_kind:
			print "Wrong model kind !, this model needs config of kind '%s', but a '%s' config is given." % (self.model_kind, self.conf.model_kind)
			exit(0)

		self.name = name
		self.job_type = job_type
		self.task_id = int(task_id)
		self.dtype = dtype

		# data stub 
		self.sess = None
		self.train_set = []
		self.dev_set = []
		self.dequeue_data_op = None 
		self.prev_max_dev_loss = 10000000
		self.latest_train_losses = []

		self.learning_rate = None
		self.learning_rate_decay_op = None

		self.global_step = None 
		self.trainable_params = []
		self.global_params = []
		self.optimizer_params = []
		self.need_init = []
		self.saver = None
		self.builder = None

		self.run_options = None #tf.RunOptions(trace_level=tf.RunOptions.FULL_TRACE)
		self.run_metadata = None #tf.RunMetadata()

	def apply_deploy_conf(self, deploy_conf):
		self.conf.variants = deploy_conf.get("variants", self.conf.variants)
		self.conf.output_max_len = deploy_conf.get("max_out", self.conf.output_max_len)
		self.conf.input_max_len = deploy_conf.get("max_in", self.conf.input_max_len)
		self.conf.max_res_num = deploy_conf.get("max_res", self.conf.max_res_num)
		self.conf.beam_splits = deploy_conf.get("beam_splits", self.conf.beam_splits)
		self.conf.stddev = deploy_conf.get("stddev", self.conf.stddev)
		self.conf.restore_from = deploy_conf.get("rf", self.conf.restore_from)

	@abc.abstractmethod 
	def build(self, for_deploy):
		"""build graph in deploy/train way

		build a graph with two seperate graph branches for both training and inference,
		this function will return a graph_nodes dict in which some specific keys
		are offered.
		
		Params:
			for_deploy: to specify the current branch to build(train or deploy)
			variants: a additional specification for model variants,
		Returns:
			graph_nodes: a dict with following keys:
				{
					"loss":...,
					"inputs":...,
					"outputs":...,
					"debug_outputs":...,
					"visualize":...
				}
				Typically, in training paradigm, loss is required, inputs and outputs are none;
				While in infer paradigm, loss is none and inputs and outputs are required;
				visuallize and debug_outputs are always optional.
				Note that in training paradigm, an 'update' key mapped to a backprop op upon
				graph_nodes['loss'] will be also added to graph_nodes after this function is
				called in build_all(...) 
		"""
		return

	@abc.abstractmethod
	def get_init_ops():
		return

	def init_fn(self):
		init_ops = self.get_init_ops()
		def fn(scaffold, sess):
			graphlg.info("Saver not used, created model with fresh parameters.")
			graphlg.info("initialize new models")
			for each in init_ops:
				graphlg.info("initialize op: %s" % str(each))
				sess.run(each)
		return fn

	@abc.abstractmethod
	def get_restorer(self):
		return
	
	@abc.abstractmethod
	def export(self, sess, nodes, version, deploy_dir="deployments"):

		# global steps as version
		export_dir = os.path.join(os.path.join(deploy_dir, self.name), str(version))

		if os.path.exists(export_dir):
			print("Removing duplicate: %s" % export_dir)
			shutil.rmtree(export_dir)

		inputs = {k:utils.build_tensor_info(v) for k, v in nodes["inputs"].items()}
		outputs = {k:utils.build_tensor_info(v) for k, v in nodes["outputs"].items()}
		
		print "==== INPUTS ===="
		print inputs
		print "==== OUTPUTS ===="
		print outputs
		signature_def = signature_def_utils.build_signature_def(inputs=inputs,
				outputs=outputs, 
				method_name=tf.saved_model.signature_constants.PREDICT_METHOD_NAME)
		
		builder = saved_model_builder.SavedModelBuilder(export_dir)
		builder.add_meta_graph_and_variables(sess,
				[tag_constants.SERVING],
				signature_def_map={
						signature_constants.DEFAULT_SERVING_SIGNATURE_DEF_KEY:signature_def
					}
				)
		builder.save()
		print('Exporting trained model to %s' % export_dir)
		return

	@abc.abstractmethod
	def after_proc(self, out):
		return {}


	@abc.abstractmethod
	def print_after_proc(self, after_proc):
		for i, each in enumerate(after_proc):
			if isinstance(each, list):
				for res in each:
					final = " ".join(res["outputs"])
					res["outputs"] = ""
					print "[%d]" % i, final, res
			else:				
				final = " ".join(each["outputs"])
				each["outputs"] = ""	
				print "[%d]" % i, final, each

	def fetch_data(self, use_random=False, begin=0, size=128, dev=False, sess=None):
		""" General Fetch data process
		"""
		if self.conf.use_data_queue:
			if sess:
				curr_sess = sess
			elif self.sess == None:
				print "FATAL: The model must be initialized first when data queue used !!"  
				exit(0)
			elif self.dequeue_data_op == None:
				print "FATAL: 'use_data_queue' in conf is true but dequeue_data_op is None"
				exit(0)
			else:
				curr_sess = self.sess
			examples = curr_sess.run(self.dequeue_data_op)
		else:
			records = self.dev_set if dev else self.train_set
			if use_random == True:
				examples = random.sample(records, size)
			else:
				begin = begin % len(records)
				examples = records[begin:begin+size]
		return examples

	def build_all(self, for_deploy, device="/cpu:0"):
		graphlg.info("Building main graph...")	
		with tf.device(device):
			#with variable_scope.variable_scope(self.model_kind, dtype=tf.float32) as scope: 
			inputs = self.build_inputs(for_deploy)
			graph_nodes = self.build(inputs, for_deploy)

			graphlg.info("Collecting trainable params...")
			self.trainable_params.extend(tf.trainable_variables())
			if not for_deploy:	
				graphlg.info("Creating backpropagation graph and optimizers...")
				graph_nodes["update"] = self.backprop(graph_nodes["loss"])
				graph_nodes["summary"] = tf.summary.merge_all()
			if "visualize" not in graph_nodes:
				graph_nodes["visualize"] = None
			graphlg.info("Graph done")
			graphlg.info("")
		self.saver = tf.train.Saver(max_to_keep=self.conf.max_to_keep)

		# More log info about device placement and params memory
		devices = {}
		for each in tf.trainable_variables():
			if each.device not in devices:
				devices[each.device] = []
			graphlg.info("%s, %s, %s" % (each.name, each.get_shape(), each.device))
			devices[each.device].append(each)
		mem = []
		graphlg.info(" ========== Params placment ==========")
		for d in devices:
			tmp = 0.0
			for each in devices[d]: 
				#graphlg.info("%s, %s, %s" % (d, each.name, each.get_shape()))
				shape = each.get_shape()
				size = 1.0 
				for dim in shape:
					size *= int(dim)
				tmp += size
			mem.append("Device: %s, Param size: %s MB" % (d, tmp * self.dtype.size / 1024.0 / 1024.0))
		graphlg.info(" ========== Device Params Mem ==========")
		for each in mem:
			graphlg.info(each)
		return graph_nodes

	def backprop(self, loss):
		# Backprop graph and optimizers
		conf = self.conf
		dtype = self.dtype
		with tf.name_scope("%s/%s" % (self.model_kind, self.name)):
			self.learning_rate = tf.Variable(float(conf.learning_rate),
									trainable=False, name="learning_rate")
			self.learning_rate_decay_op = self.learning_rate.assign(
						self.learning_rate * conf.learning_rate_decay_factor)

			self.global_step = tf.Variable(0, trainable=False, name="global_step")
			self.data_idx = tf.Variable(0, trainable=False, name="data_idx")
			self.data_idx_inc_op = self.data_idx.assign(self.data_idx + conf.batch_size)
		with tf.name_scope("backprop") as scope:
			self.optimizers = {
				"SGD":tf.train.GradientDescentOptimizer(self.learning_rate),
				"Adadelta":tf.train.AdadeltaOptimizer(self.learning_rate),
				"Adagrad":tf.train.AdagradOptimizer(self.learning_rate),
				"AdagradDA":tf.train.AdagradDAOptimizer(self.learning_rate, self.global_step),
				"Moment":tf.train.MomentumOptimizer(self.learning_rate, 0.9),
				"Ftrl":tf.train.FtrlOptimizer(self.learning_rate),
				"RMSProp":tf.train.RMSPropOptimizer(self.learning_rate),
				"Adam":tf.train.AdamOptimizer(self.learning_rate),
				"COCOB":COCOB()
			}

			self.opt = self.optimizers[conf.opt_name]
			tmp = set(tf.global_variables()) 

			if self.job_type == "worker": 
				self.opt = tf.train.SyncReplicasOptimizer(self.opt, conf.replicas_to_aggregate, conf.total_num_replicas) 
				grads_and_vars = self.opt.compute_gradients(loss=loss) 
				gradients, variables = zip(*grads_and_vars)  
			else:
				gradients = tf.gradients(loss, tf.trainable_variables(), aggregation_method=2)
				variables = tf.trainable_variables()

			clipped_gradients, self.grad_norm = tf.clip_by_global_norm(gradients, conf.max_gradient_norm)
			update = self.opt.apply_gradients(zip(clipped_gradients, variables), self.global_step)

			graphlg.info("Collecting optimizer params and global params...")
			self.optimizer_params.append(self.learning_rate)
			self.optimizer_params.extend(list(set(tf.global_variables()) - tmp))
			self.global_params.extend([self.global_step, self.data_idx])
			tf.add_to_collection(tf.GraphKeys.GLOBAL_STEP, self.global_step)
		return update

	def preproc(self, records, for_deploy=False, use_seg=False, default_wgt=1.0):
		# parsing
		data = []
		for each in records:
			if not for_deploy or self.conf.variants == "score":
				segs = re.split("\t", each.strip())
				if len(segs) < 2:
					continue
				p, r = segs[0], segs[1]
				p_list, r_list = re.split(" +", p), re.split(" +", r)
				if self.conf.reverse:
					p_list, r_list = r_list, p_list

				down_wgts = segs[-1] if len(segs) > 2 else default_wgt 
				data.append([p_list, len(p_list) + 1, r_list, len(r_list) + 1, down_wgts])
			else:
				p = each.strip()
				p_list, _ = tokenize_word(p) if use_seg else (re.split(" +", p), None)
				data.append([p_list, len(p_list) + 1, [], 1, 1.0])

		# batching
		conf = self.conf
		batch_enc_inps, batch_dec_inps, batch_enc_lens, batch_dec_lens, batch_down_wgts = [], [], [], [], []
		for encs, enc_len, decs, dec_len, down_wgts in data:
			# Encoder inputs are padded, reversed and then padded to max.
			enc_len = enc_len if enc_len < conf.input_max_len else conf.input_max_len
			encs = encs[0:conf.input_max_len]
			if conf.enc_reverse:
				encs = list(reversed(encs + ["_PAD"] * (enc_len - len(encs))))
			enc_inps = encs + ["_PAD"] * (conf.input_max_len - len(encs))

			batch_enc_inps.append(enc_inps)
			batch_enc_lens.append(np.int32(enc_len))
			if not for_deploy or self.conf.variants == "score":
				# Decoder inputs with an extra "GO" symbol and "EOS_ID", then padded.
				decs += ["_EOS"]
				decs = decs[0:conf.output_max_len + 1]
				# fit to the max_dec_len
				if dec_len > conf.output_max_len + 1:
					dec_len = conf.output_max_len + 1
				# Merge dec inps and targets 
				batch_dec_inps.append(["_GO"] + decs + ["_PAD"] * (conf.output_max_len + 1 - len(decs)))
				batch_dec_lens.append(np.int32(dec_len))
				if not self.conf.variants == "score":
					batch_down_wgts.append(down_wgts)

		self.curr_input_feed = feed_dict = {
			"enc_inps:0": batch_enc_inps,
			"enc_lens:0": batch_enc_lens,
			"dec_inps:0": batch_dec_inps,
			"dec_lens:0": batch_dec_lens,
			"down_wgts:0": batch_down_wgts
		}
		for k, v in feed_dict.items():
			if not v: 
				del feed_dict[k]
		return feed_dict

	def adjust_lr_rate(self, global_step, step_loss):
		self.latest_train_losses.append(step_loss)
		if step_loss < self.latest_train_losses[0]:
			self.latest_train_losses = self.latest_train_losses[-1:] 
		if global_step > self.conf.lr_keep_steps and len(self.latest_train_losses) == self.conf.lr_check_steps:
			self.sess.run(self.learning_rate_decay_op)
			self.latest_train_losses = []


	def visualize(self, train_root, sess, graph_nodes, records=[], use_seg=False, ckpt_steps=None): 
		if "visualize" not in graph_nodes:
			print "visualize nodes not found"
			return

		# tf variables and temp variables to hold embs
		embs = {}
		emb_vars = {}
		for start in range(0, len(records), self.conf.batch_size):
			print "Runing examples %d - %d..." % (start, start + self.conf.batch_size)
			batch = records[start:start + self.conf.batch_size]
			input_feed = self.preproc(batch, use_seg=use_seg, for_deploy=True)
			#visuals, outputs = sess.run([graph_nodes["visualize"], graph_nodes["outputs"]], feed_dict=input_feed)
			visuals = sess.run(graph_nodes["visualize"], feed_dict=input_feed)
			for k, v in visuals.items():
				if k not in embs:
					embs[k] = []
				embs[k].append(v)
		for k,v in graph_nodes["visualize"].items():
			dim_size = int(tf.contrib.layers.flatten(v).get_shape()[1])
			emb_vars[k] = tf.Variable(tf.random_normal([len(records), dim_size]), name=k)

		ckpt_dir = os.path.join(train_root, self.name)
		#emb_dir = ckpt_dir + "-embs" 
		emb_dir = os.path.join(ckpt_dir, "embeddings")
		#meta_path = os.path.join(ckpt_dir, "metadata.tsv")
		#meta_path = os.path.join(train_root, "metadata.tsv")


		# do embedding
		meta_path = os.path.join(emb_dir, "metadata.tsv")
		config = projector.ProjectorConfig()
		#summary_writer = tf.summary.FileWriter(os.path.join(train_root, self.name))
		summary_writer = tf.summary.FileWriter(emb_dir, sess.graph)
		print "all keys: %s" % str(embs.keys())
		for node_name, emb_list in embs.items():
			print "Embedding %s..." % node_name
			## may not be used
			#outs = np.concatenate(out_list, axis=0)
			sess.run(emb_vars[node_name].assign(np.concatenate(emb_list, axis=0)))

			embedding = config.embeddings.add()
			embedding.tensor_name = emb_vars[node_name].name 
			embedding.metadata_path =  "metadata.tsv" 

		#saver = tf.train.Saver(emb_vars.values())
		saver = tf.train.Saver(emb_vars.values())
		#saver.save(sess, os.path.join(ckpt_dir, "embeddings"), 0)
		#saver.save(sess, os.path.join(train_root, "embs"), 0)
		saver.save(sess, os.path.join(emb_dir, "embs.ckpt"), 0)
		projector.visualize_embeddings(summary_writer, config)

		# writing metadata
		print "Writing meta data %s..." % meta_path
		with codecs.open(meta_path, "w") as f:
			f.write("Query\tFrequency\n")
			#for i, each in enumerate(outs):
			#	each = list(each)
			#	if "_EOS" in each:
			#		each = each[0:each.index("_EOS")]
			#	f.write("%s --> %s\t%d\n" % (records[i], "".join(each), i))
			for i, line in enumerate(records):
				f.write("%s\t%d\n" % (line, 1))
		return
		
	def dummy_train(self, sess, graph_nodes):
		print "Dequeue one batch..."
		batch_records = self.fetch_data(sess=sess) 
		N = 10 
		while True:
			print "Step only on one batch..."
			feed_dict = self.preproc(batch_records, for_deploy=False)
			t0 = time.time()
			fetches = {
				"loss":graph_nodes["loss"],
				"update":graph_nodes["update"],
				"debug_outputs":graph_nodes["debug_outputs"]
			}
			out = sess.run(fetches, feed_dict)
			t = time.time() - t0
			for i in range(N):
				print "=================="
				for key in feed_dict:
					if isinstance(feed_dict[key][i], list):
						print "%s_%d:" % (key, i), " ".join([str(each) for each in feed_dict[key][i]])
					else:
						print "%s_%d:" % (key, i), str(feed_dict[key][i])
				if isinstance(out["debug_outputs"], dict):
					for k,v in out["debug_outputs"].items():
						print ">>> %s_%d:" % (k, i), " ".join(v[i])
				else:
					print ">>> debug_outputs_%d:" % i, " ".join(out["debug_outputs"][i])
			print "TIME: %.4f, LOSS: %.10f" % (t, out["loss"])
			print ""

	def test(self, sess, graph_nodes, use_seg=True):
		if self.conf.variants == "":
			while True:
				query = raw_input(">>")
				batch_records = [query]
				feed_dict = self.preproc(batch_records, use_seg=use_seg, for_deploy=True)
				print "[feed_dict]", feed_dict
				out_dict = sess.run(graph_nodes["outputs"], feed_dict) 
				out = self.after_proc(out_dict)
				self.print_after_proc(out)
		elif self.conf.variants == "score":
			while True:
				post = raw_input("Post >>")
				resp = raw_input("Response >>")
				words, _ = tokenize_word(resp) if use_seg else (resp.split(), None)
				resp_str = " ".join(words)
				print "Score resp: %s" % resp_str
				batch_records = ["%s\t%s" % (post, resp_str)]
				feed_dict = self.preproc(batch_records, use_seg=use_seg, for_deploy=True)
				print "[feed_dict]", feed_dict
				out_dict = sess.run(graph_nodes["outputs"], feed_dict)
				prob = out_dict["logprobs"]
				print prob
