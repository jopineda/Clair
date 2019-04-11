import sys
import os
import time
import argparse
import logging
import pickle
import numpy as np

import param
import utils
import clair_model as cv
from utils import VariantLength

logging.basicConfig(format='%(message)s', level=logging.INFO)
base2num = dict(zip("ACGT", (0, 1, 2, 3)))
num2base = dict(zip((0, 1, 2, 3), "ACGT"))


def f1_score(confusion_matrix):
    column_sum = confusion_matrix.sum(axis=0)
    row_sum = confusion_matrix.sum(axis=1)

    f1_score_array = np.array([])
    matrix_size = confusion_matrix.shape[0]
    for i in range(matrix_size):
        TP = confusion_matrix[i][i] + 0.0
        precision = TP / column_sum[i]
        recall = TP / row_sum[i]
        f1_score_array = np.append(f1_score_array, (2.0 * precision * recall) / (precision + recall))

    return f1_score_array


def evaluate_model(m, dataset_info):
    dataset_size = dataset_info["dataset_size"]
    x_array_compressed = dataset_info["x_array_compressed"]
    y_array_compressed = dataset_info["y_array_compressed"]

    logging.info("[INFO] Testing on the training and validation dataset ...")
    prediction_start_time = time.time()
    prediction_batch_size = param.predictBatchSize
    # no_of_training_examples = int(dataset_size*param.trainingDatasetPercentage)
    # validation_data_start_index = no_of_training_examples + 1

    base_predictions = []
    genotype_predictions = []
    indel_length_predictions_1 = []
    indel_length_predictions_2 = []

    dataset_index = 0
    end_flag = 0

    while dataset_index < dataset_size:
        if end_flag != 0:
            break

        x_batch, _, end_flag = utils.decompress_array(
            x_array_compressed, dataset_index, prediction_batch_size, dataset_size)
        minibatch_base_prediction, minibatch_genotype_prediction, \
            minibatch_indel_length_prediction_1, minibatch_indel_length_prediction_2 = m.predict(x_batch)

        base_predictions.append(minibatch_base_prediction)
        genotype_predictions.append(minibatch_genotype_prediction)
        indel_length_predictions_1.append(minibatch_indel_length_prediction_1)
        indel_length_predictions_2.append(minibatch_indel_length_prediction_2)

        dataset_index += prediction_batch_size

    base_predictions = np.concatenate(base_predictions[:])
    genotype_predictions = np.concatenate(genotype_predictions[:])
    indel_length_predictions_1 = np.concatenate(indel_length_predictions_1[:])
    indel_length_predictions_2 = np.concatenate(indel_length_predictions_2[:])

    logging.info("[INFO] Prediciton time elapsed: %.2f s" % (time.time() - prediction_start_time))

    # Evaluate the trained model
    y_array, _, _ = utils.decompress_array(y_array_compressed, 0, dataset_size, dataset_size)

    logging.info("[INFO] Evaluation on base change:")

    print("[INFO] Evaluation on base change:")
    all_base_count = top_1_count = top_2_count = 0
    confusion_matrix = np.zeros((21, 21), dtype=np.int)
    for base_change_prediction, base_change_label in zip(base_predictions, y_array[:, 0:21]):
        confusion_matrix[np.argmax(base_change_label)][np.argmax(base_change_prediction)] += 1

        all_base_count += 1
        indexes_with_sorted_prediction_probability = base_change_prediction.argsort()[::-1]
        if np.argmax(base_change_label) == indexes_with_sorted_prediction_probability[0]:
            top_1_count += 1
            top_2_count += 1
        elif np.argmax(base_change_label) == indexes_with_sorted_prediction_probability[1]:
            top_2_count += 1

    print("[INFO] all/top1/top2/top1p/top2p: %d/%d/%d/%.2f/%.2f" %
          (all_base_count, top_1_count, top_2_count,
           float(top_1_count)/all_base_count*100, float(top_2_count)/all_base_count*100))
    for i in range(21):
        print("\t".join([str(confusion_matrix[i][j]) for j in range(21)]))
    base_change_f_measure = f1_score(confusion_matrix)
    print("[INFO] f-measure: ", base_change_f_measure)

    # Genotype
    print("\n[INFO] Evaluation on Genotype:")
    confusion_matrix = np.zeros((3, 3), dtype=np.int)
    for genotype_prediction, true_genotype_label in zip(genotype_predictions, y_array[:, 21:24]):
        confusion_matrix[np.argmax(true_genotype_label)][np.argmax(genotype_prediction)] += 1
    for epoch_count in range(3):
        print("\t".join([str(confusion_matrix[epoch_count][j]) for j in range(3)]))
    genotype_f_measure = f1_score(confusion_matrix)
    print("[INFO] f-measure: ", genotype_f_measure)

    # Indel length 1
    print("\n[INFO] evaluation on indel length 1:")
    confusion_matrix = np.zeros((VariantLength.output_label_count, VariantLength.output_label_count), dtype=np.int)
    for indel_length_prediction_1, true_indel_length_label_1 in zip(indel_length_predictions_1, y_array[:, 24:57]):
        confusion_matrix[np.argmax(true_indel_length_label_1)][np.argmax(indel_length_prediction_1)] += 1
    for i in range(VariantLength.output_label_count):
        print("\t".join([str(confusion_matrix[i][j]) for j in range(VariantLength.output_label_count)]))
    indel_length_f_measure_1 = f1_score(confusion_matrix)
    print("[INFO] f-measure: ", indel_length_f_measure_1)

    # Indel length 2
    print("\n[INFO] evaluation on indel length 2:")
    confusion_matrix = np.zeros((VariantLength.output_label_count, VariantLength.output_label_count), dtype=np.int)
    for indel_length_prediction_2, true_indel_length_label_2 in zip(indel_length_predictions_2, y_array[:, 57:90]):
        confusion_matrix[np.argmax(true_indel_length_label_2)][np.argmax(indel_length_prediction_2)] += 1
    for i in range(VariantLength.output_label_count):
        print("\t".join([str(confusion_matrix[i][j]) for j in range(VariantLength.output_label_count)]))
    indel_length_f_measure_2 = f1_score(confusion_matrix)
    print("[INFO] f-measure: ", indel_length_f_measure_2)


if __name__ == "__main__":

    parser = argparse.ArgumentParser(description="Evaluate trained Clair model")

    parser.add_argument('--bin_fn', type=str, default=None,
                        help="Binary tensor input generated by tensor2Bin.py, tensor_fn, var_fn and bed_fn will be ignored")

    parser.add_argument('--tensor_fn', type=str, default="vartensors",
                        help="Tensor input")

    parser.add_argument('--var_fn', type=str, default="truthvars",
                        help="Truth variants list input")

    parser.add_argument('--bed_fn', type=str, default=None,
                        help="High confident genome regions input in the BED format")

    parser.add_argument('--chkpnt_fn', type=str, default=None,
                        help="Input a checkpoint for testing, REQUIRED")

    args = parser.parse_args()

    if len(sys.argv[1:]) == 0:
        parser.print_help()
        sys.exit(1)

    # initialize
    logging.info("[INFO] Loading model ...")
    utils.setup_environment()

    m = cv.Clair()
    m.init()

    dataset_info = utils.dataset_info_from(
        binary_file_path=args.bin_fn,
        tensor_file_path=args.tensor_fn,
        variant_file_path=args.var_fn,
        bed_file_path=args.bed_fn
    )

    model_initalization_file_path = args.chkpnt_fn
    m.restore_parameters(os.path.abspath(model_initalization_file_path))

    # start evaluation
    evaluate_model(m, dataset_info)
