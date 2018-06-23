import argparse
import sys

from nltk.translate.bleu_score import corpus_bleu, SmoothingFunction 
import numpy as np
import tensorflow as tf
from tensorflow.python.layers import core as layers_core
from tensorflow.python import debug as tf_debug
from tqdm import tqdm

from project.external.nmt import bleu
from project.utils.tokenize import PAD_TOKEN, UNKNOWN_TOKEN, \
                               START_OF_TEXT_TOKEN, END_OF_TEXT_TOKEN


EXPERIMENT_SUMMARY_STRING = '''
--------------------------------------------
--------------------------------------------
DATA: vocab_size: {voc}, char_seq: {char},
       desc_seq: {desc}, full_dataset: {full}
--------------------------------------------
{model}
--------------------------------------------
--------------------------------------------
'''


class BasicRNNModel(object):

    summary_string = 'MODEL: {classname}\nName: {name}\n\n{summary}'

    def __init__(self, word2idx, word_weights, char2idx, char_weights, 
                 rnn_size=300, batch_size=128, learning_rate=0.001, name="BasicModel"):
        # To Do; all these args from config, to make saving model easier.
        self.name = name

        self.word_weights = word_weights
        self.char_weights = char_weights

        self.word2idx = word2idx
        self.idx2word = dict((v,k) for k,v in word2idx.items())
        self.char2idx = char2idx
        self.idx2char = dict((v,k) for k,v in char2idx.items())


        self.batch_size = batch_size
        self.learning_rate = learning_rate
        self.rnn_size = rnn_size

        # Graph Variables (built later)
        self.input_data_sequence = None
        self.input_label_sequence = None
        self.update = None
        self.loss = None

        self._build_train_graph()

        print("Init loaded")

    def arg_summary(self):
        mod_args =  "ModArgs: rnn_size: {}, lr: {}, batch_size: {}, ".format(
            self.rnn_size, self.learning_rate, self.batch_size)
        
        data_args =  "DataArgs: vocab_size: {}, char_embed: {}, word_embed: {}, ".format(
            len(self.word2idx), self.char_weights.shape[1], self.word_weights.shape[1])
        return "\n".join([mod_args, data_args])

    def __str__(self):
        return self.__class__.summary_string.format(
                name=self.name, classname=self.__class__.__name__, summary=self.arg_summary())

    @staticmethod
    def _build_encode_decode_embeddings(input_data_sequence, char_weights,
                                        input_label_sequence, word_weights):
        with tf.name_scope("embed_vars"):
            # 1. Embed Our "arg_names" char by char
            char_vocab_size, char_embed_size = char_weights.shape
            char_initializer =  tf.constant_initializer(char_weights)
            char_embedding = tf.get_variable("char_embed", [char_vocab_size, char_embed_size],
                                             initializer=char_initializer, trainable=True)
            encode_embedded = tf.nn.embedding_lookup(char_embedding, input_data_sequence)

            # 2. Embed Our "arg_desc" word by word
            desc_vocab_size, word_embed_size = word_weights.shape
            word_initializer = tf.constant_initializer(word_weights)
            word_embedding = tf.get_variable("desc_embed", [desc_vocab_size, word_embed_size],
                                             initializer=word_initializer, trainable=False)
            decode_embedded = tf.nn.embedding_lookup(word_embedding, input_label_sequence)

            return encode_embedded, decode_embedded, char_embedding, word_embedding

    @staticmethod
    def _build_rnn_encoder(input_data_seq_length, rnn_size, encode_embedded):
        with tf.name_scope("encoder"):
            batch_size = tf.shape(input_data_seq_length)
            encoder_rnn_cell = tf.contrib.rnn.BasicLSTMCell(rnn_size, name="RNNencoder")
            initial_state = encoder_rnn_cell.zero_state(batch_size, dtype=tf.float32)

            return tf.nn.dynamic_rnn(encoder_rnn_cell, encode_embedded,
                                        sequence_length=input_data_seq_length,
                                        initial_state=initial_state, time_major=False)

    @staticmethod
    def _build_rnn_training_decoder(decoder_rnn_cell, state, projection_layer, decoder_weights,
                                    input_label_seq_length, decode_embedded):
        with tf.name_scope("training"):
            batch_size = tf.shape(state[0])[0]
            
            helper = tf.contrib.seq2seq.TrainingHelper(
                     decode_embedded, input_label_seq_length, time_major=False)
            
            decoder_initial_state = decoder_rnn_cell.zero_state(batch_size, dtype=tf.float32).clone(
                cell_state=state)

            decoder = tf.contrib.seq2seq.BasicDecoder(
                              decoder_rnn_cell, helper, decoder_initial_state,
                              output_layer=projection_layer)

            return tf.contrib.seq2seq.dynamic_decode(decoder, impute_finished=True)

    @staticmethod
    def _build_rnn_greedy_inference_decoder(decoder_rnn_cell, state, projection_layer, decoder_weights,
                                            start_tok, end_tok):
        with tf.name_scope("inference"):
            batch_size = tf.shape(state[0])[0]

            helper = tf.contrib.seq2seq.GreedyEmbeddingHelper(decoder_weights,
                tf.fill([batch_size], start_tok), end_tok)
            
            decoder_initial_state = decoder_rnn_cell.zero_state(batch_size, dtype=tf.float32).clone(
                cell_state=state)

            decoder = tf.contrib.seq2seq.BasicDecoder(
                           decoder_rnn_cell, helper, decoder_initial_state,
                           output_layer=projection_layer)

            maximum_iterations = 300
            return tf.contrib.seq2seq.dynamic_decode(
                        decoder, impute_finished=True, maximum_iterations=maximum_iterations)

    @staticmethod
    def _get_loss(logits, input_label_sequence, input_label_seq_length):
        with tf.name_scope("loss"):
            batch_size = tf.shape(input_label_sequence)[0]
            zero_col = tf.zeros([batch_size,1], dtype=tf.int32)

            # Shift the decoder to be the next word, and then clip it
            decoder_outputs = tf.concat([input_label_sequence[:, 1:], zero_col], 1)  # TODO transform this
            maximum_length = tf.reduce_max(input_label_seq_length)
            decoder_outputs = decoder_outputs[:,:maximum_length]

            crossent = tf.nn.sparse_softmax_cross_entropy_with_logits(
                           labels=decoder_outputs, logits=logits)

            target_weights = tf.logical_not(tf.equal(decoder_outputs, tf.zeros_like(decoder_outputs)))
            target_weights = tf.cast(target_weights, tf.float32)
            train_loss = (tf.reduce_sum(crossent * target_weights) / tf.cast(batch_size, tf.float32))
        return train_loss

    @staticmethod
    def _do_updates(train_loss, learning_rate):
        with tf.name_scope("opt"):
            # Clip the gradients
            max_gradient_norm = 1
            params = tf.trainable_variables()
            gradients = tf.gradients(train_loss, params)
            clipped_gradients, _ = tf.clip_by_global_norm(
                                    gradients, max_gradient_norm)

            # Create Optimiser and Apply Update
            optimizer = tf.train.AdamOptimizer(learning_rate)
            update = optimizer.apply_gradients(zip(clipped_gradients, params))
        return update

    def _build_train_graph(self):
        with tf.name_scope("Model_{}".format(self.name)):
            # 0. Define our placeholders and derived vars
            # # input_data_sequence : [batch_size x max_variable_length]
            input_data_sequence = tf.placeholder(tf.int32, [None, None], "arg_name")
            input_data_seq_length = tf.argmin(input_data_sequence, axis=1, output_type=tf.int32) + 1
            # # input_label_sequence  : [batch_size x max_docstring_length]
            input_label_sequence = tf.placeholder(tf.int32, [None, None], "arg_desc")
            input_label_seq_length = tf.argmin(input_label_sequence, axis=1, output_type=tf.int32) + 1

            # 1. Get Embeddings
            encode_embedded, decode_embedded, _, decoder_weights = self._build_encode_decode_embeddings(
                                                    input_data_sequence, self.char_weights,
                                                    input_label_sequence, self.word_weights)

            # 2. Build out Encoder
            encoder_outputs, state = self._build_rnn_encoder(input_data_seq_length, self.rnn_size, encode_embedded)

            # 3. Build out Cell ith attention
            decoder_rnn_cell = tf.contrib.rnn.BasicLSTMCell(self.rnn_size, name="RNNencoder")

            desc_vocab_size, _ = self.word_weights.shape
            projection_layer = layers_core.Dense(desc_vocab_size, use_bias=False)

            attention_mechanism = tf.contrib.seq2seq.LuongAttention(
                self.rnn_size, encoder_outputs,
                memory_sequence_length=input_data_seq_length)
            
            decoder_rnn_cell = tf.contrib.seq2seq.AttentionWrapper(
                decoder_rnn_cell, attention_mechanism,
                attention_layer_size=self.rnn_size)

            # 4. Build out helpers
            train_outputs, _, _ = self._build_rnn_training_decoder(decoder_rnn_cell,
                                                    state,projection_layer, decoder_weights, input_label_seq_length,
                                                    decode_embedded)

            inf_outputs, _, _ = self._build_rnn_greedy_inference_decoder(decoder_rnn_cell,
                                                    state,projection_layer, decoder_weights,
                                                    self.word2idx[START_OF_TEXT_TOKEN],
                                                    self.word2idx[END_OF_TEXT_TOKEN])
            

            # 5. Define Train Loss
            train_logits = train_outputs.rnn_output
            train_loss = self._get_loss(train_logits, input_label_sequence, input_label_seq_length)
            train_translate = train_outputs.sample_id
            
            # 6. Define Translation
            inf_logits = inf_outputs.rnn_output
            inf_translate = inf_outputs.sample_id
            inf_loss = self._get_loss(inf_logits, input_label_sequence, input_label_seq_length)


            # 7. Do Updates
            update = self._do_updates(train_loss, self.learning_rate)

            # 8. Save Variables to Model
            self.input_data_sequence = input_data_sequence
            self.input_label_sequence = input_label_sequence
            self.update = update
            self.train_loss = train_loss
            self.train_id = train_translate

            self.inference_loss = inf_loss
            self.inference_id = inf_translate

    def translate(self, translate_id, filter_pad=True, lookup=None, do_join=True):
        if lookup is None:
            lookup = self.idx2word
        if filter_pad:
            translate_id = np.trim_zeros(translate_id, 'b')
        
        if do_join:
            return  " ".join([lookup[i] for i in translate_id])
        else:
            return [lookup[i] for i in translate_id]


    def _feed_fwd(self, session, input_data, input_labels, operation):
        """
        Evaluates a node in the graph
        Args
            session: session that is being run
            input_data, array: batch of comments
            input_labels, array: batch of labels
            operation: node in graph to be evaluated
        Returns
            output of the operation
        """
        run_ouputs = operation
        feed_dict = {self.input_data_sequence: input_data,
                     self.input_label_sequence: input_labels}

        return session.run(run_ouputs, feed_dict=feed_dict)

    def _to_batch(self, arg_name, arg_desc, epochs=1e5, do_prog_bar=False):
        assert arg_name.shape[0] == arg_desc.shape[0]
        size = arg_name.shape[0]

        batch_per_epoch = (size // self.batch_size) + 1

        zipped = list(zip(arg_name, arg_desc))
        np.random.shuffle(zipped)
        arg_name, arg_desc = zip(*zipped)

        for i in tqdm(range(batch_per_epoch * epochs), disable=True):
            idx_start = (i % batch_per_epoch) * self.batch_size
            idx_end = ( (i % batch_per_epoch) + 1)  * self.batch_size

            arg_name_batch = arg_name[idx_start: idx_end]
            arg_desc_batch = arg_desc[idx_start: idx_end]
            yield arg_name_batch, arg_desc_batch

    def evaluate_model(self, session, test_data, data_limit, test_translate=0):
        all_translations = []
        all_references = []
        all_training_loss = 0
        for test_arg_name, test_arg_desc in self._to_batch(test_data[0][:data_limit], test_data[1][:data_limit], 1):
            train_loss, inference_ids = self._feed_fwd(session, test_arg_name, test_arg_desc, [self.train_loss, self.inference_id])
            all_training_loss += train_loss

            translations = [self.translate(i, do_join=False) for i in inference_ids]
            reference = [[self.translate(i, do_join=False)] for i in test_arg_desc]

            all_translations.extend(translations)
            all_references.extend(reference)

        bleu_tuple = bleu.compute_bleu(all_references, all_translations, max_order=4, smooth=True)
        bleu_score1, precisions, bp, ratio, translation_length, reference_length = bleu_tuple
        bleu_tuple = bleu.compute_bleu(all_references, all_translations, max_order=4, smooth=False)
        bleu_score3, precisions, bp, ratio, translation_length, reference_length = bleu_tuple
        
        smoother = SmoothingFunction()
        bleu_score2 = corpus_bleu(all_references, all_translations, smoothing_function=smoother.method2)
        bleu_score4 = corpus_bleu(all_references, all_translations, smoothing_function=smoother.method0)

        sample_translations = self.sample_translation(session, test_data, test_translate)

        bleu_score = "NMT Smth: {:.3f} NLTK Smth: {:.3f} NMT: {:.3f} NLTK: {:.3f}".format(
            bleu_score1*100, bleu_score2*100, bleu_score3*100, bleu_score4*100)

        return bleu_score, sample_translations, all_training_loss

    def sample_translation(self, session, data, translation_count):
        arg_names, arg_descs = data
        zipped = list(zip(arg_names, arg_descs))
        np.random.shuffle(zipped)
        arg_names, arg_descs = zip(*zipped)
        if translation_count <= 0 :
            return ""
        else:
            ops = [self.inference_id]
            [inference_ids] = self._feed_fwd(session, arg_names[:translation_count], arg_descs[:translation_count], ops)

            results = []
            for i in range(translation_count):
                arg_name = self.translate(arg_names[i], lookup=self.idx2char).replace(" ","")
                arg_desc = self.translate(arg_descs[i])
                inference_desc = START_OF_TEXT_TOKEN + " " + self.translate(inference_ids[i])
    
                string = "----\nARGN: {}\nDESC: {}\nINFR: {}".format(arg_name, arg_desc, inference_desc)
                results.append(string)
            return "\n".join(results)

    def main(self, session, epochs, train_data, test_data=None, test_check=20, test_translate=0):

        for i, (arg_name, arg_desc) in enumerate(self._to_batch(*train_data, epochs)):

                ops = [self.update, self.train_loss, self.train_id]
                _,  _, train_id = self._feed_fwd(session, arg_name, arg_desc, ops)

                if i % test_check == 0:
                    

                    train_bleu, train_trans, train_loss = self.evaluate_model(session, train_data, 10000, test_translate)
                    if test_data is not None:
                        test_bleu, test_trans, test_loss = self.evaluate_model(session, test_data, 10000, test_translate)
                    print("---------------------------------------------")
                    
                    print("TRAINING: {}".format(train_bleu))
                    print(train_trans)
                    print('--------------------')

                    print("TEST: {}".format(test_bleu))
                    print(test_trans)
                    print('--------------------')
                    print("MINIBATCHES: {}, TRAIN_LOSS: {}, TEST_LOSS: {},\nTRAIN_BLEU: {}\nTEST_BLEU: {}".format(
                        i, train_loss, test_loss, train_bleu, test_bleu))
                    sys.stdout.flush() # remove when adding a logger



def _build_argparser():
    parser = argparse.ArgumentParser(description='Run the basic LSTM model on the overfit dataset')
    parser.add_argument('--lstm-size', '-l', dest='lstm_size', action='store',
                        type=int, default=300,
                        help='size of LSTM size')
    parser.add_argument('--learning-rate', '-r', dest='lr', action='store',
                        type=float, default=0.001,
                        help='learning rate for model')
    parser.add_argument('--batch-size', '-b', dest='batch_size', action='store',
                        type=int, default=128,
                        help='minibatch size for model')
    parser.add_argument('--epochs', '-e', dest='epochs', action='store',
                        type=int, default=5000,
                        help='minibatch size for model')
    parser.add_argument('--vocab-size', '-v', dest='vocab_size', action='store',
                        type=int, default=50000,
                        help='size of embedding vocab')
    parser.add_argument('--char-seq', '-c', dest='char_seq', action='store',
                        type=int, default=24,
                        help='max char sequence length')
    parser.add_argument('--desc-seq', '-d', dest='desc_seq', action='store',
                        type=int, default=120,
                        help='max desecription sequence length')
    parser.add_argument('--test-freq', '-t', dest='test_freq', action='store',
                        type=int, default=100,
                        help='how often to run a test and dump output')
    parser.add_argument('--dump-translation', '-D', dest='test_translate', action='store',
                        type=int, default=5,
                        help='dump extensive test information on each test batch')
    parser.add_argument('--use-full-dataset', '-F', dest='use_full_dataset', action='store_true',
                        default=False,
                        help='dump extensive test information on each test batch')
    return parser

def _run_model(lstm_size, lr, batch_size, vocab_size, char_seq, desc_seq,
                    test_freq, use_full_dataset, test_translate, epochs):
    if use_full_dataset:
        from project.data.preprocessed import data as DATA
    else:
        from project.data.preprocessed.overfit import data as DATA

    import project.utils.tokenize as tokenize

    print("Loading GloVe weights and word to index lookup table")
    word_weights, word2idx = tokenize.get_weights_word2idx(vocab_size)
    print("Creating char to index look up table")
    char_weights, char2idx = tokenize.get_weights_char2idx()

    print("Tokenizing the word desctiptions and characters")
    train_data = tokenize.tokenize_descriptions(DATA.train, word2idx, char2idx)
    test_data = tokenize.tokenize_descriptions(DATA.test, word2idx, char2idx)
    print("Extracting tensors train and test")
    train_data = tokenize.extract_char_and_desc_idx_tensors(train_data, char_seq, desc_seq)
    test_data = tokenize.extract_char_and_desc_idx_tensors(test_data, char_seq, desc_seq)

    nn = BasicRNNModel(word2idx, word_weights, char2idx, char_weights,
                        lstm_size, batch_size, lr)

    summary = EXPERIMENT_SUMMARY_STRING.format(voc=vocab_size, char=char_seq,
                                           desc=desc_seq, full=use_full_dataset,
                                           model=nn)
    print(summary)

    init = tf.group(tf.global_variables_initializer(), tf.local_variables_initializer())

    session_conf = tf.ConfigProto(
      intra_op_parallelism_threads=4,
      inter_op_parallelism_threads=4)

    sess = tf.Session(config=session_conf)
    sess.run(init)

    nn.main(sess, epochs, train_data, test_data, test_check=test_freq, test_translate=test_translate)


if __name__=="__main__":
    parser = _build_argparser()
    args = parser.parse_args()

    _run_model(**vars(args))

