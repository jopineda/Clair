import sys
import os
import time
import argparse
import param
import logging
import numpy as np
from threading import Thread
from math import log, e
from enum import IntEnum
from collections import namedtuple, defaultdict
from itertools import izip

import utils
import clair_model as cv
from utils import GT21, base_change_label_from, Genotype, genotype_string_from, VariantLength

import pysam

logging.basicConfig(format='%(message)s', level=logging.INFO)
num2base = dict(zip((0, 1, 2, 3), "ACGT"))
base2num = dict(zip("ACGT", (0, 1, 2, 3)))
minimum_variant_length_that_need_infer = VariantLength.max
maximum_variant_length_that_need_infer = 50
inferred_indel_length_minimum_allele_frequency = 0.125
flanking_base_number = param.flankingBaseNum

OutputConfig = namedtuple('OutputConfig', [
    'is_show_reference',
    'is_debug',
    'quality_score_for_pass',
])
OutputUtilities = namedtuple('OutputUtilities', [
    'print_debug_message',
    'insertion_bases_using',
    'deletion_bases_using',
    'insertion_bases_using_pysam_using',
    'output',
    'output_header',
    'close_opened_files',
])


class Channel(IntEnum):
    reference = 0
    insert = 1
    delete = 2
    SNP = 3


def homo_SNP_bases_from(base_change_probabilities):
    output_bases_probabilities = np.array([
        base_change_probabilities[GT21.AA],
        base_change_probabilities[GT21.CC],
        base_change_probabilities[GT21.GG],
        base_change_probabilities[GT21.TT],
    ])
    output_bases = [
        base_change_label_from(GT21.AA),
        base_change_label_from(GT21.CC),
        base_change_label_from(GT21.GG),
        base_change_label_from(GT21.TT)
    ][np.argmax(output_bases_probabilities)]
    return output_bases[0], output_bases[1]


def hetero_SNP_bases_from(base_change_probabilities):
    output_bases_probabilities = np.array([
        base_change_probabilities[GT21.AC],
        base_change_probabilities[GT21.AG],
        base_change_probabilities[GT21.AT],
        base_change_probabilities[GT21.CG],
        base_change_probabilities[GT21.CT],
        base_change_probabilities[GT21.GT]
    ])
    output_bases = [
        base_change_label_from(GT21.AC),
        base_change_label_from(GT21.AG),
        base_change_label_from(GT21.AT),
        base_change_label_from(GT21.CG),
        base_change_label_from(GT21.CT),
        base_change_label_from(GT21.GT)
    ][np.argmax(output_bases_probabilities)]
    return output_bases[0], output_bases[1]


def filtration_value_from(quality_score_for_pass, quality_score):
    if quality_score_for_pass is None:
        return "."
    if quality_score >= quality_score_for_pass:
        return "PASS"
    return "LowQual"


def pileup(sam_file, contig, position_start, position_end, func):
    """
    Pileup using pysam

    sam_file: pysam.AlignmentFile for pileup
    contig: chromosome name or contig name
    position_start: start position. 0-based. Inclusive.
    position_end: ending position. 0-based. Exclusive.
    func: callback for pileup_column
    """
    try:
        for pileup_column in sam_file.pileup(
            contig,
            start=position_start,
            stop=position_end,
            flag_filter=2308,
            min_base_quality=0,
            max_depth=250
        ):
            func(pileup_column)
    except AssertionError:
        pass


def insertion_bases_using_pysam_from(
    sam_file,
    contig,
    position,
    minimum_insertion_length=1,
    maximum_insertion_length=maximum_variant_length_that_need_infer,
    insertion_bases_to_ignore=""
):
    insertion_bases_dict = defaultdict(lambda: 0)

    def lambda_function(pileup_column):
        if pileup_column.reference_pos != position - 1:
            return

        for sequence in pileup_column.get_query_sequences(mark_matches=False, mark_ends=False, add_indels=True):
            # minimum sequence needed: A+1A, and "+" for insertion
            if len(sequence) < 4 or sequence[1] != "+":
                continue

            no_of_insertion_bases = 0
            for (string_index, c) in enumerate(sequence[2:]):
                if not c.isdigit():
                    insertion_bases = sequence[string_index+2:].upper()
                    break
                no_of_insertion_bases = no_of_insertion_bases * 10 + int(c)

            if (
                minimum_insertion_length <= no_of_insertion_bases <= maximum_insertion_length and
                insertion_bases != insertion_bases_to_ignore
            ):
                insertion_bases_dict[insertion_bases] = insertion_bases_dict[insertion_bases] + 1
    pileup(sam_file, contig, position, position+1, func=lambda_function)

    return max(insertion_bases_dict, key=insertion_bases_dict.get) if len(insertion_bases_dict) > 0 else ""


def deletion_bases_using_pysam_from(
    sam_file,
    fasta_file,
    contig,
    position,
    minimum_deletion_length=1,
    maximum_deletion_length=maximum_variant_length_that_need_infer
):
    deletion_bases_dict = defaultdict(lambda: 0)

    def lambda_function(pileup_column):
        if pileup_column.reference_pos != position - 1:
            return

        for sequence in pileup_column.get_query_sequences(mark_matches=False, mark_ends=False, add_indels=True):
            # minimum sequence needed: A-1A, and "-" for deletion
            if len(sequence) < 4 or sequence[1] != "-":
                continue

            no_of_deletion_bases = 0
            for c in sequence[2:]:
                if not c.isdigit():
                    deletion_bases = fasta_file.fetch(
                        reference=contig, start=position, end=position + no_of_deletion_bases
                    )
                    break
                no_of_deletion_bases = no_of_deletion_bases * 10 + int(c)

            if minimum_deletion_length <= no_of_deletion_bases <= maximum_deletion_length:
                deletion_bases_dict[deletion_bases] = deletion_bases_dict[deletion_bases] + 1
    pileup(sam_file, contig, position, position+1, func=lambda_function)

    return max(deletion_bases_dict, key=deletion_bases_dict.get) if len(deletion_bases_dict) > 0 else ""


def Run(args):
    utils.setup_environment()

    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"
    os.environ["NUMEXPR_NUM_THREADS"] = "1"

    if args.threads == None:
        if args.tensor_fn == "PIPE":
            param.NUM_THREADS = 4
    else:
        param.NUM_THREADS = args.threads
        param.NUM_THREADS -= 1
        if param.NUM_THREADS < 1:
            param.NUM_THREADS = 1

    m = cv.Clair()
    m.init()
    m.restore_parameters(os.path.abspath(args.chkpnt_fn))

    if args.activation_only:
        log_activation(args, m)
    else:
        call_variants(args, m)


def output_utilties_from(
    sample_name,
    is_debug,
    is_using_pysam_for_all_indel_bases_output,
    bam_file_path,
    reference_file_path,
    output_file_path,
):
    fasta_file = pysam.FastaFile(filename=reference_file_path) if reference_file_path else None
    sam_file = pysam.AlignmentFile(bam_file_path, mode="rb")
    output_file = open(output_file_path, "w")

    def output(string_value):
        print >> output_file, string_value

    def print_debug_message(
        chromosome,
        position,
        base_change_probabilities,
        genotype_probabilities,
        variant_length_probabilities_1,
        variant_length_probabilities_2,
        extra_infomation_string=""
    ):
        if not is_debug:
            return

        output("{}\t{}\t{}\t{}\t{}\t{}\t{}".format(
            chromosome,
            position,
            ["{:0.8f}".format(x) for x in base_change_probabilities],
            ["{:0.8f}".format(x) for x in genotype_probabilities],
            ["{:0.8f}".format(x) for x in variant_length_probabilities_1],
            ["{:0.8f}".format(x) for x in variant_length_probabilities_2],
            extra_infomation_string
        ))

    def insertion_bases_using(tensor_input, variant_length, contig, position):
        return insertion_bases_from(
            sam_file=sam_file,
            tensor_input=tensor_input,
            variant_length=variant_length,
            contig=contig,
            position=position,
            is_using_pysam_for_all_indel_bases_output=is_using_pysam_for_all_indel_bases_output
        )

    def deletion_bases_using(tensor_input, variant_length, contig, position, reference_sequence):
        return deletion_bases_from(
            tensor_input=tensor_input,
            variant_length=variant_length,
            sam_file=sam_file,
            fasta_file=fasta_file,
            contig=contig,
            position=position,
            reference_sequence=reference_sequence,
            is_using_pysam_for_all_indel_bases_output=is_using_pysam_for_all_indel_bases_output
        )

    def insertion_bases_using_pysam_using(
        contig,
        position,
        minimum_insertion_length,
        maximum_insertion_length,
        insertion_bases_to_ignore
    ):
        return insertion_bases_using_pysam_from(
            sam_file=sam_file,
            contig=contig,
            position=position,
            minimum_insertion_length=minimum_insertion_length,
            maximum_insertion_length=maximum_insertion_length,
            insertion_bases_to_ignore=insertion_bases_to_ignore
        )

    def close_opened_files():
        sam_file.close()
        fasta_file.close()
        output_file.close()

    def output_header():
        from textwrap import dedent
        output(dedent("""\
            ##fileformat=VCFv4.1
            ##FILTER=<ID=PASS,Description="All filters passed">
            ##FILTER=<ID=LowQual,Description="Confidence in this variant being real is below calling threshold.">
            ##ALT=<ID=DEL,Description="Deletion">
            ##ALT=<ID=INS,Description="Insertion of novel sequence">
            ##INFO=<ID=SVTYPE,Number=1,Type=String,Description="Type of structural variant">
            ##INFO=<ID=LENGUESS,Number=.,Type=Integer,Description="Best guess of the indel length">
            ##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">
            ##FORMAT=<ID=GQ,Number=1,Type=Integer,Description="Genotype Quality">
            ##FORMAT=<ID=DP,Number=1,Type=Integer,Description="Read Depth">
            ##FORMAT=<ID=AF,Number=1,Type=Float,Description="Estimated allele frequency in the range (0,1)">"""
        ))

        if reference_file_path is not None:
            reference_index_file_path = reference_file_path + ".fai"
            with open(reference_index_file_path, "r") as fai_fp:
                for row in fai_fp:
                    columns = row.strip().split("\t")
                    contig_name, contig_size = columns[0], columns[1]
                    output("##contig=<ID=%s,length=%s>" % (contig_name, contig_size))

        output('#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t%s' % (sample_name))

    return OutputUtilities(
        print_debug_message,
        insertion_bases_using,
        deletion_bases_using,
        insertion_bases_using_pysam_using,
        output,
        output_header,
        close_opened_files,
    )


def homo_Ins_tuples_from(variant_length_probabilities_1, variant_length_probabilities_2, extra_probability):
    return [(
        i,
        variant_length_probabilities_1[i + VariantLength.index_offset] *
        variant_length_probabilities_2[i + VariantLength.index_offset] * extra_probability
    ) for i in xrange(1, VariantLength.max + 1)]


def hetero_Ins_tuples_from(variant_length_probabilities_1, variant_length_probabilities_2):
    return [(
        i,
        max(
            variant_length_probabilities_1[0 + VariantLength.index_offset] *
            variant_length_probabilities_2[i + VariantLength.index_offset],
            variant_length_probabilities_1[i + VariantLength.index_offset] *
            variant_length_probabilities_2[0 + VariantLength.index_offset],
        )
    ) for i in xrange(1, VariantLength.max + 1)]


def hetero_InsIns_tuples_from(variant_length_probabilities_1, variant_length_probabilities_2, extra_probability):
    probabilities = []
    for i in xrange(1, VariantLength.max + 1):
        for j in xrange(1, VariantLength.max + 1):
            # note: one kind of InsIns is same # of insertion bases but different kind of ACGT
            probabilities.append((
                (i, j) if i <= j else (j, i),
                variant_length_probabilities_1[i + VariantLength.index_offset] *
                variant_length_probabilities_2[j + VariantLength.index_offset] * extra_probability
            ))
    return probabilities


def homo_Del_tuples_from(variant_length_probabilities_1, variant_length_probabilities_2, extra_probability):
    return [(
        i,
        variant_length_probabilities_1[-i + VariantLength.index_offset] *
        variant_length_probabilities_2[-i + VariantLength.index_offset] * extra_probability
    ) for i in xrange(1, VariantLength.max + 1)]


def hetero_Del_tuples_from(variant_length_probabilities_1, variant_length_probabilities_2):
    return [(
        i,
        max(
            variant_length_probabilities_1[0 + VariantLength.index_offset] *
            variant_length_probabilities_2[-i + VariantLength.index_offset],
            variant_length_probabilities_1[-i + VariantLength.index_offset] *
            variant_length_probabilities_2[0 + VariantLength.index_offset],
        )
    ) for i in xrange(1, VariantLength.max + 1)]


def hetero_DelDel_tuples_from(variant_length_probabilities_1, variant_length_probabilities_2, extra_probability):
    probabilities = []
    for i in xrange(1, VariantLength.max + 1):
        for j in xrange(1, VariantLength.max + 1):
            if i == j:
                continue
            probabilities.append((
                (i, j) if i < j else (j, i),
                variant_length_probabilities_1[-i + VariantLength.index_offset] *
                variant_length_probabilities_2[-j + VariantLength.index_offset] * extra_probability
            ))
    return probabilities


def hetero_InsDel_tuples_from(variant_length_probabilities_1, variant_length_probabilities_2, extra_probability):
    probabilities = []
    for i in xrange(1, VariantLength.max + 1):
        for j in xrange(1, VariantLength.max + 1):
            probabilities.append((
                (j, i),
                variant_length_probabilities_1[i + VariantLength.index_offset] *
                variant_length_probabilities_2[-j + VariantLength.index_offset] * extra_probability
            ))
            probabilities.append((
                (i, j),
                variant_length_probabilities_1[-i + VariantLength.index_offset] *
                variant_length_probabilities_2[j + VariantLength.index_offset] * extra_probability
            ))
    return probabilities


def inferred_insertion_bases_from(tensor_input):
    insertion_bases = ""
    for position in xrange(flanking_base_number + 1, 2 * flanking_base_number + 1):
        reference_tensor = tensor_input[position, :, Channel.reference]
        insertion_tensor = np.copy(tensor_input[position, :, Channel.insert])
        for base_index in range(0, 4):
            insertion_tensor[base_index] = insertion_tensor[base_index] + insertion_tensor[base_index + 4]
            insertion_tensor[base_index + 4] = 0
        if (
            position < (flanking_base_number + minimum_variant_length_that_need_infer) or
            sum(insertion_tensor) >= inferred_indel_length_minimum_allele_frequency * sum(reference_tensor)
        ):
            insertion_bases += num2base[np.argmax(insertion_tensor) % 4]
        else:
            break
    return insertion_bases


def inferred_deletion_length_from(tensor_input):
    deletion_length = 0
    for position in xrange(flanking_base_number + 1, 2 * flanking_base_number + 1):
        reference_tensor = tensor_input[position, :, Channel.reference]
        deletion_tensor = tensor_input[position, :, Channel.delete]
        if (
            position < (flanking_base_number + minimum_variant_length_that_need_infer) or
            sum(deletion_tensor) >= inferred_indel_length_minimum_allele_frequency * sum(reference_tensor)
        ):
            deletion_length += 1
        else:
            break
    return deletion_length


def insertion_bases_using_tensor(tensor_input, variant_length):
    insertion_bases = ""
    for position in range(flanking_base_number + 1, flanking_base_number + variant_length + 1):
        insertion_tensor = np.copy(tensor_input[position, :, Channel.insert])
        for base_index in range(0, 4):
            insertion_tensor[base_index] = insertion_tensor[base_index] + insertion_tensor[base_index + 4]
            insertion_tensor[base_index + 4] = 0
        insertion_bases += num2base[np.argmax(insertion_tensor) % 4]
    return insertion_bases


def maximum_variant_length_from(variant_length):
    if variant_length >= minimum_variant_length_that_need_infer:
        return maximum_variant_length_that_need_infer
    else:
        return variant_length


def insertion_bases_from(
    tensor_input,
    variant_length,
    sam_file,
    contig,
    position,
    is_using_pysam_for_all_indel_bases_output
):
    """
        Return (insertion_bases, insertion bases length) tuple
    """
    if is_using_pysam_for_all_indel_bases_output:
        insertion_bases = insertion_bases_using_pysam_from(
            sam_file=sam_file,
            contig=contig,
            position=position,
            minimum_insertion_length=variant_length,
            maximum_insertion_length=maximum_variant_length_from(variant_length)
        )
        return insertion_bases, len(insertion_bases)

    need_inferred_variant_length = variant_length >= minimum_variant_length_that_need_infer
    if not need_inferred_variant_length:
        insertion_bases = insertion_bases_using_tensor(tensor_input, variant_length)
        return insertion_bases, len(insertion_bases)

    insertion_bases = insertion_bases_using_pysam_from(
        sam_file=sam_file,
        contig=contig,
        position=position,
        minimum_insertion_length=minimum_variant_length_that_need_infer
    )
    insertion_length = len(insertion_bases)
    if insertion_length > 0:
        return insertion_bases, insertion_length
    else:
        insertion_bases = inferred_insertion_bases_from(tensor_input)
        return insertion_bases, len(insertion_bases)


def deletion_bases_from(
    tensor_input,
    variant_length,
    sam_file,
    fasta_file,
    contig,
    position,
    reference_sequence,
    is_using_pysam_for_all_indel_bases_output
):
    """
        Return (deletion_bases, deletion bases length) tuple
    """
    if is_using_pysam_for_all_indel_bases_output:
        deletion_bases = deletion_bases_using_pysam_from(
            sam_file=sam_file,
            fasta_file=fasta_file,
            contig=contig,
            position=position,
            minimum_deletion_length=variant_length,
            maximum_deletion_length=maximum_variant_length_from(variant_length)
        )
        return deletion_bases, len(deletion_bases)

    deletion_bases = ""
    need_inferred_variant_length = variant_length >= minimum_variant_length_that_need_infer
    if need_inferred_variant_length:
        deletion_bases = deletion_bases_using_pysam_from(
            sam_file=sam_file,
            fasta_file=fasta_file,
            contig=contig,
            position=position,
            minimum_deletion_length=minimum_variant_length_that_need_infer
        )

    have_long_deletion_bases = need_inferred_variant_length and len(deletion_bases) >= flanking_base_number
    if not have_long_deletion_bases:
        deletion_bases = reference_sequence[flanking_base_number + 1:flanking_base_number + variant_length + 1]
    return deletion_bases, len(deletion_bases)


def quality_score_from(
    reference_base,
    alternate_base,
    genotype_string,
    gt21_probabilities,
    genotype_probabilities,
):
    genotype_1, genotype_2 = int(genotype_string[0]), int(genotype_string[2])
    if genotype_1 > genotype_2:
        genotype_1, genotype_2 = genotype_2, genotype_1

    alternate_arr = alternate_base.split(',')
    if len(alternate_arr) == 1:
        alternate_arr = (
            [reference_base if genotype_1 == 0 or genotype_2 == 0 else alternate_arr[0]] +
            alternate_arr
        )
    partial_labels = [utils.partial_label_from(reference_base, alternate) for alternate in alternate_arr]
    gt21_label = utils.mix_two_partial_labels(partial_labels[0], partial_labels[1])
    gt21 = utils.base_change_enum_from(gt21_label)

    is_homo_reference = genotype_1 == 0 and genotype_2 == 0
    is_homo_variant = not is_homo_reference and genotype_1 == genotype_2
    is_hetero_variant = not is_homo_reference and not is_homo_variant
    is_multi = not is_homo_variant and genotype_1 != 0 and genotype_2 != 0
    genotype = Genotype.unknown
    if is_homo_reference:
        genotype = Genotype.homo_reference
    elif is_homo_variant:
        genotype = Genotype.homo_variant
    elif is_hetero_variant and not is_multi:
        genotype = Genotype.hetero_variant
    elif is_hetero_variant and is_multi:
        genotype = Genotype.hetero_variant
        # genotype = Genotype.hetero_variant_multi
    else:
        return 0

    p = gt21_probabilities[gt21] * genotype_probabilities[genotype]
    tmp = max(
        (-10 * log(e, 10)) * log(((1.0 - p) + 1e-300) / (p + 1e-300)) + 16,
        0
    )

    return int(round(tmp * tmp))


def possible_outcome_probabilites_from(
    gt21_probabilities,
    genotype_probabilities,
    variant_length_probabilities_1,
    variant_length_probabilities_2,
    reference_base,
):
    homo_reference_probability = genotype_probabilities[Genotype.homo_reference]
    homo_variant_probability = genotype_probabilities[Genotype.homo_variant]
    hetero_variant_probability = genotype_probabilities[Genotype.hetero_variant]
    variant_length_0_probability = (
        variant_length_probabilities_1[0 + VariantLength.index_offset] *
        variant_length_probabilities_2[0 + VariantLength.index_offset]
    )

    reference_gt21 = utils.base_change_enum_from(reference_base + reference_base)
    homo_Ref_probability = (
        variant_length_0_probability * homo_reference_probability * gt21_probabilities[reference_gt21]
    )

    homo_SNP_probabilities = [(
        variant_length_0_probability * homo_variant_probability * gt21_probabilities[gt21]
    ) for gt21 in [GT21.AA, GT21.CC, GT21.GG, GT21.TT]]
    hetero_SNP_probabilities = [(
        variant_length_0_probability * hetero_variant_probability * gt21_probabilities[gt21]
    ) for gt21 in [GT21.AC, GT21.AG, GT21.AT, GT21.CG, GT21.CT, GT21.GT]]

    # Insertion
    homo_Ins_lengths, homo_Ins_probabilities = zip(*homo_Ins_tuples_from(
        variant_length_probabilities_1, variant_length_probabilities_2,
        homo_variant_probability * gt21_probabilities[GT21.InsIns]
    ))
    homo_Ins_lengths, homo_Ins_probabilities = list(homo_Ins_lengths), list(homo_Ins_probabilities)
    hetero_InsIns_length_tuples, hetero_InsIns_probabilities = zip(*hetero_InsIns_tuples_from(
        variant_length_probabilities_1, variant_length_probabilities_2,
        hetero_variant_probability * gt21_probabilities[GT21.InsIns]
    ))
    hetero_InsIns_length_tuples, hetero_InsIns_probabilities = (
        list(hetero_InsIns_length_tuples), list(hetero_InsIns_probabilities)
    )
    hetero_ACGT_Ins_tuples = []
    for length_tuples, p in hetero_Ins_tuples_from(variant_length_probabilities_1, variant_length_probabilities_2):
        for gt21, hetero_base in [(GT21.AIns, "A"), (GT21.CIns, "C"), (GT21.GIns, "G"), (GT21.TIns, "T")]:
            hetero_ACGT_Ins_tuples.append((
                hetero_base,
                length_tuples,
                p * gt21_probabilities[gt21] * hetero_variant_probability
            ))
    hetero_ACGT_Ins_bases, hetero_ACGT_Ins_lengths, hetero_ACGT_Ins_probabilities = zip(*hetero_ACGT_Ins_tuples)
    hetero_ACGT_Ins_bases, hetero_ACGT_Ins_lengths, hetero_ACGT_Ins_probabilities = (
        list(hetero_ACGT_Ins_bases), list(hetero_ACGT_Ins_lengths), list(hetero_ACGT_Ins_probabilities)
    )

    # Deletion
    homo_Del_lengths, homo_Del_probabilities = zip(*homo_Del_tuples_from(
        variant_length_probabilities_1, variant_length_probabilities_2,
        homo_variant_probability * gt21_probabilities[GT21.DelDel]
    ))
    homo_Del_lengths, homo_Del_probabilities = list(homo_Del_lengths), list(homo_Del_probabilities)
    hetero_DelDel_length_tuples, hetero_DelDel_probabilities = zip(*hetero_DelDel_tuples_from(
        variant_length_probabilities_1, variant_length_probabilities_2,
        hetero_variant_probability * gt21_probabilities[GT21.DelDel]
    ))
    hetero_DelDel_length_tuples, hetero_DelDel_probabilities = (
        list(hetero_DelDel_length_tuples), list(hetero_DelDel_probabilities)
    )
    hetero_ACGT_Del_tuples = []
    for length_tuples, p in hetero_Del_tuples_from(variant_length_probabilities_1, variant_length_probabilities_2):
        for gt21, hetero_base in [(GT21.ADel, "A"), (GT21.CDel, "C"), (GT21.GDel, "G"), (GT21.TDel, "T")]:
            hetero_ACGT_Del_tuples.append((
                hetero_base,
                length_tuples,
                p * gt21_probabilities[gt21] * hetero_variant_probability
            ))
    hetero_ACGT_Del_bases, hetero_ACGT_Del_lengths, hetero_ACGT_Del_probabilities = zip(*hetero_ACGT_Del_tuples)
    hetero_ACGT_Del_bases, hetero_ACGT_Del_lengths, hetero_ACGT_Del_probabilities = (
        list(hetero_ACGT_Del_bases), list(hetero_ACGT_Del_lengths), list(hetero_ACGT_Del_probabilities)
    )

    # InsDel
    hetero_InsDel_length_tuples, hetero_InsDel_probabilities = zip(*hetero_InsDel_tuples_from(
        variant_length_probabilities_1, variant_length_probabilities_2,
        hetero_variant_probability * gt21_probabilities[GT21.InsDel]
    ))
    hetero_InsDel_length_tuples, hetero_InsDel_probabilities = (
        list(hetero_InsDel_length_tuples), list(hetero_InsDel_probabilities)
    )

    return (
        homo_Ref_probability,
        homo_SNP_probabilities,
        hetero_SNP_probabilities,
        homo_Ins_lengths, homo_Ins_probabilities,
        hetero_InsIns_length_tuples, hetero_InsIns_probabilities,
        hetero_ACGT_Ins_bases, hetero_ACGT_Ins_lengths, hetero_ACGT_Ins_probabilities,
        homo_Del_lengths, homo_Del_probabilities,
        hetero_DelDel_length_tuples, hetero_DelDel_probabilities,
        hetero_ACGT_Del_bases, hetero_ACGT_Del_lengths, hetero_ACGT_Del_probabilities,
        hetero_InsDel_length_tuples, hetero_InsDel_probabilities,
    )


def output_from(
    x,
    reference_sequence,
    contig,
    position,
    tensor_position_center,
    gt21_probabilities,
    genotype_probabilities,
    variant_length_probabilities_1,
    variant_length_probabilities_2,
    output_utilities,
):
    insertion_bases_using, deletion_bases_using, insertion_bases_using_pysam_using = (
        output_utilities.insertion_bases_using,
        output_utilities.deletion_bases_using,
        output_utilities.insertion_bases_using_pysam_using,
    )

    (
        homo_Ref_probability,
        homo_SNP_probabilities,
        hetero_SNP_probabilities,
        homo_Ins_lengths, homo_Ins_probabilities,
        hetero_InsIns_length_tuples, hetero_InsIns_probabilities,
        hetero_ACGT_Ins_bases, hetero_ACGT_Ins_lengths, hetero_ACGT_Ins_probabilities,
        homo_Del_lengths, homo_Del_probabilities,
        hetero_DelDel_length_tuples, hetero_DelDel_probabilities,
        hetero_ACGT_Del_bases, hetero_ACGT_Del_lengths, hetero_ACGT_Del_probabilities,
        hetero_InsDel_length_tuples, hetero_InsDel_probabilities,
    ) = possible_outcome_probabilites_from(
        gt21_probabilities,
        genotype_probabilities,
        variant_length_probabilities_1,
        variant_length_probabilities_2,
        reference_base=reference_sequence[tensor_position_center],
    )

    reference_base, alternate_base = None, None
    while reference_base is None or alternate_base is None:
        maximum_probability = max(
            homo_Ref_probability,
            max(homo_SNP_probabilities),
            max(hetero_SNP_probabilities),
            max(homo_Ins_probabilities) if len(homo_Ins_probabilities) else 0,
            max(homo_Del_probabilities) if len(homo_Del_probabilities) else 0,
            max(hetero_ACGT_Ins_probabilities) if len(hetero_ACGT_Ins_probabilities) else 0,
            max(hetero_InsIns_probabilities) if len(hetero_InsIns_probabilities) else 0,
            max(hetero_ACGT_Del_probabilities) if len(hetero_ACGT_Del_probabilities) else 0,
            max(hetero_DelDel_probabilities) if len(hetero_DelDel_probabilities) else 0,
            max(hetero_InsDel_probabilities) if len(hetero_InsDel_probabilities) else 0,
        )

        is_reference = maximum_probability == homo_Ref_probability
        if is_reference:
            return (
                (True, False, False, False, False, False, False, False, False, False),
                (reference_sequence[tensor_position_center], reference_sequence[tensor_position_center])
            )

        is_homo_SNP = maximum_probability in homo_SNP_probabilities
        is_hetero_SNP = maximum_probability in hetero_SNP_probabilities
        is_homo_insertion = maximum_probability in homo_Ins_probabilities
        is_hetero_ACGT_Ins = maximum_probability in hetero_ACGT_Ins_probabilities
        is_hetero_InsIns = maximum_probability in hetero_InsIns_probabilities
        is_homo_deletion = maximum_probability in homo_Del_probabilities
        is_hetero_ACGT_Del = maximum_probability in hetero_ACGT_Del_probabilities
        is_hetero_DelDel = maximum_probability in hetero_DelDel_probabilities
        is_insertion_and_deletion = maximum_probability in hetero_InsDel_probabilities

        if is_homo_SNP:
            base1, base2 = homo_SNP_bases_from(gt21_probabilities)
            reference_base = reference_sequence[tensor_position_center]
            alternate_base = base1 if base1 != reference_base else base2

        elif is_hetero_SNP:
            base1, base2 = hetero_SNP_bases_from(gt21_probabilities)
            reference_base = reference_sequence[tensor_position_center]
            is_multi = base1 != reference_base and base2 != reference_base
            if is_multi:
                alternate_base = "{},{}".format(base1, base2)
            else:
                alternate_base = base1 if base1 != reference_base else base2

        elif is_homo_insertion:
            idx = homo_Ins_probabilities.index(maximum_probability)
            variant_length = homo_Ins_lengths[idx]
            del homo_Ins_probabilities[idx]
            del homo_Ins_lengths[idx]

            insertion_bases, insertion_length = insertion_bases_using(
                tensor_input=x, variant_length=variant_length, contig=contig, position=position
            )
            if insertion_length == 0:
                continue
            reference_base = reference_sequence[tensor_position_center]
            alternate_base = reference_base + insertion_bases

        elif is_hetero_ACGT_Ins:
            idx = hetero_ACGT_Ins_probabilities.index(maximum_probability)
            variant_length = hetero_ACGT_Ins_lengths[idx]
            hetero_Ins_base = hetero_ACGT_Ins_bases[idx]
            del hetero_ACGT_Ins_probabilities[idx]
            del hetero_ACGT_Ins_lengths[idx]
            del hetero_ACGT_Ins_bases[idx]

            insertion_bases, insertion_length = insertion_bases_using(
                tensor_input=x, variant_length=variant_length, contig=contig, position=position
            )
            if insertion_length == 0:
                continue
            reference_base = reference_sequence[tensor_position_center]
            alternate_base = reference_base + insertion_bases

            is_SNP_Ins_multi = hetero_Ins_base != reference_base
            if is_SNP_Ins_multi:
                alternate_base = "{},{}".format(hetero_Ins_base, alternate_base)

        elif is_hetero_InsIns:
            idx = hetero_InsIns_probabilities.index(maximum_probability)
            variant_length_1, variant_length_2 = hetero_InsIns_length_tuples[idx]
            del hetero_InsIns_probabilities[idx]
            del hetero_InsIns_length_tuples[idx]

            insertion_bases, insertion_length = insertion_bases_using(
                tensor_input=x, variant_length=variant_length_2, contig=contig, position=position
            )
            if insertion_length == 0:
                continue
            reference_base = reference_sequence[tensor_position_center]
            alternate_base = reference_base + insertion_bases

            another_insertion_bases = (
                insertion_bases_using_pysam_using(
                    contig=contig,
                    position=position,
                    minimum_insertion_length=variant_length_1,
                    maximum_insertion_length=maximum_variant_length_from(variant_length_1),
                    insertion_bases_to_ignore=insertion_bases
                ) or
                insertion_bases[0:variant_length_1]
            )
            alternate_base_1 = reference_base + another_insertion_bases
            alternate_base_2 = alternate_base
            if alternate_base_1 != alternate_base_2:
                alternate_base = "{},{}".format(alternate_base_1, alternate_base_2)
            else:
                reference_base, alternate_base = None, None

        elif is_homo_deletion:
            idx = homo_Del_probabilities.index(maximum_probability)
            variant_length = homo_Del_lengths[idx]
            del homo_Del_probabilities[idx]
            del homo_Del_lengths[idx]

            deletion_bases, deletion_length = deletion_bases_using(
                tensor_input=x,
                variant_length=variant_length,
                contig=contig,
                position=position,
                reference_sequence=reference_sequence,
            )
            if deletion_length == 0:
                continue
            reference_base = reference_sequence[tensor_position_center] + deletion_bases
            alternate_base = reference_base[0]

        elif is_hetero_ACGT_Del:
            idx = hetero_ACGT_Del_probabilities.index(maximum_probability)
            variant_length = hetero_ACGT_Del_lengths[idx]
            hetero_Del_base = hetero_ACGT_Del_bases[idx]
            del hetero_ACGT_Del_probabilities[idx]
            del hetero_ACGT_Del_lengths[idx]
            del hetero_ACGT_Del_bases[idx]

            deletion_bases, deletion_length = deletion_bases_using(
                tensor_input=x,
                variant_length=variant_length,
                contig=contig,
                position=position,
                reference_sequence=reference_sequence,
            )
            if deletion_length == 0:
                continue
            reference_base = reference_sequence[tensor_position_center] + deletion_bases
            alternate_base = reference_base[0]

            is_SNP_Del_multi = hetero_Del_base != reference_base[0]
            if is_SNP_Del_multi:
                alternate_base_1 = alternate_base
                alternate_base_2 = hetero_Del_base + reference_base[1:]
                alternate_base = "{},{}".format(alternate_base_1, alternate_base_2)

        elif is_hetero_DelDel:
            idx = hetero_DelDel_probabilities.index(maximum_probability)
            variant_length_1, variant_length_2 = hetero_DelDel_length_tuples[idx]
            del hetero_DelDel_probabilities[idx]
            del hetero_DelDel_length_tuples[idx]

            deletion_bases, deletion_length = deletion_bases_using(
                tensor_input=x,
                variant_length=variant_length_2,
                contig=contig,
                position=position,
                reference_sequence=reference_sequence,
            )
            if deletion_length == 0:
                continue
            reference_base = reference_sequence[tensor_position_center] + deletion_bases
            alternate_base = reference_base[0]

            alternate_base_1 = alternate_base
            alternate_base_2 = reference_base[0] + reference_base[variant_length_1 + 1:]
            if (
                alternate_base_1 != alternate_base_2 and
                reference_base != alternate_base_1 and reference_base != alternate_base_2
            ):
                alternate_base = "{},{}".format(alternate_base_1, alternate_base_2)
            else:
                reference_base, alternate_base = None, None

        elif is_insertion_and_deletion:
            idx = hetero_InsDel_probabilities.index(maximum_probability)
            variant_length_1, variant_length_2 = hetero_InsDel_length_tuples[idx]
            del hetero_InsDel_probabilities[idx]
            del hetero_InsDel_length_tuples[idx]

            insertion_bases, insertion_length = insertion_bases_using(
                tensor_input=x, variant_length=variant_length_2, contig=contig, position=position
            )
            deletion_bases, deletion_length = deletion_bases_using(
                tensor_input=x,
                variant_length=variant_length_1,
                contig=contig,
                position=position,
                reference_sequence=reference_sequence,
            )
            if insertion_length == 0 or deletion_length == 0:
                continue
            reference_base = reference_sequence[tensor_position_center] + deletion_bases
            alternate_base = "{},{}".format(
                reference_base[0],
                reference_base[0] + insertion_bases + reference_base[1:]
            )

    return (
        (
            is_reference, is_homo_SNP, is_hetero_SNP,
            is_homo_insertion, is_hetero_ACGT_Ins, is_hetero_InsIns,
            is_homo_deletion, is_hetero_ACGT_Del, is_hetero_DelDel,
            is_insertion_and_deletion
        ),
        (reference_base, alternate_base)
    )


def batch_output(
    mini_batch,
    batch_base_change_probabilities,
    batch_genotype_probabilities,
    batch_variant_length_probabilities_1,
    batch_variant_length_probabilities_2,
    output_config,
    output_utilities,
):
    X, batch_chr_pos_seq = mini_batch
    batch_size = len(batch_chr_pos_seq)
    if len(batch_base_change_probabilities) != batch_size:
        sys.exit(
            "Inconsistent shape between input tensor and output predictions %d/%d" %
            (batch_size, len(batch_base_change_probabilities))
        )

    tensor_position_center = flanking_base_number
    information_string = "."

    for (
        x,
        chr_pos_seq,
        gt21_probabilities,
        genotype_probabilities,
        variant_length_probabilities_1,
        variant_length_probabilities_2
    ) in izip(
        X,
        batch_chr_pos_seq,
        batch_base_change_probabilities,
        batch_genotype_probabilities,
        batch_variant_length_probabilities_1,
        batch_variant_length_probabilities_2
    ):
        chromosome, position, reference_sequence = chr_pos_seq
        position = int(position)

        # read depth
        read_depth = sum(
            x[tensor_position_center, :, Channel.delete] + x[tensor_position_center, :, Channel.reference]
        )
        if read_depth == 0:
            output_utilities.print_debug_message(
                chromosome,
                position,
                gt21_probabilities,
                genotype_probabilities,
                variant_length_probabilities_1,
                variant_length_probabilities_2,
                "Read Depth is zero"
            )
            continue

        (
            is_reference, is_homo_SNP, is_hetero_SNP,
            is_homo_insertion, is_hetero_ACGT_Ins, is_hetero_InsIns,
            is_homo_deletion, is_hetero_ACGT_Del, is_hetero_DelDel,
            is_insertion_and_deletion
        ), (reference_base, alternate_base) = output_from(
            x,
            reference_sequence,
            chromosome,
            position,
            tensor_position_center,
            gt21_probabilities,
            genotype_probabilities,
            variant_length_probabilities_1,
            variant_length_probabilities_2,
            output_utilities,
        )

        if not output_config.is_debug and not output_config.is_show_reference and is_reference:
            continue

        if reference_base is None or alternate_base is None:
            output_utilities.print_debug_message(
                chromosome,
                position,
                gt21_probabilities,
                genotype_probabilities,
                variant_length_probabilities_1,
                variant_length_probabilities_2,
                "no reference base / alternate base prediction"
            )
            continue

        # geno type string
        if is_reference:
            genotype_string = genotype_string_from(Genotype.homo_reference)
        elif is_homo_SNP or is_homo_insertion or is_homo_deletion:
            genotype_string = genotype_string_from(Genotype.homo_variant)
        elif is_hetero_SNP or is_hetero_ACGT_Ins or is_hetero_InsIns or is_hetero_ACGT_Del or is_hetero_DelDel:
            genotype_string = genotype_string_from(Genotype.hetero_variant)
        if "," in str(alternate_base):
            genotype_string = genotype_string_from(Genotype.hetero_variant_multi)

        # allele frequency / supported reads
        is_SNP = is_homo_SNP or is_hetero_SNP
        is_insertion = is_homo_insertion or is_hetero_ACGT_Ins or is_hetero_InsIns
        is_deletion = is_homo_deletion or is_hetero_ACGT_Del or is_hetero_DelDel
        supported_reads_count = 0
        if is_reference:
            supported_reads_count = (
                x[tensor_position_center,   base2num[reference_base], Channel.reference] +
                x[tensor_position_center, base2num[reference_base]+4, Channel.reference]
            )
        elif is_SNP:
            for base in str(alternate_base):
                if base == ',':
                    continue
                supported_reads_count += (
                    x[tensor_position_center,   base2num[base], Channel.SNP] +
                    x[tensor_position_center, base2num[base]+4, Channel.SNP] +
                    x[tensor_position_center,   base2num[base], Channel.reference] +
                    x[tensor_position_center, base2num[base]+4, Channel.reference]
                )
        elif is_insertion:
            supported_reads_count = (
                sum(x[tensor_position_center+1, :, Channel.insert]) -
                sum(x[tensor_position_center+1, :, Channel.SNP])
            )
        elif is_deletion:
            supported_reads_count = sum(x[tensor_position_center+1, :, Channel.delete])
        elif is_insertion_and_deletion:
            supported_reads_count = (
                sum(x[tensor_position_center+1, :, Channel.insert]) +
                sum(x[tensor_position_center+1, :, Channel.delete]) -
                sum(x[tensor_position_center+1, :, Channel.SNP])
            )
        allele_frequency = ((supported_reads_count + 0.0) / read_depth) if read_depth != 0 else 0.0
        if allele_frequency > 1:
            allele_frequency = 1

        # quality score
        quality_score = quality_score_from(
            reference_base,
            alternate_base,
            genotype_string,
            gt21_probabilities,
            genotype_probabilities,
        )

        # filtration value
        filtration_value = filtration_value_from(
            quality_score_for_pass=output_config.quality_score_for_pass, quality_score=quality_score
        )

        if output_config.is_debug:
            output_utilities.print_debug_message(
                chromosome,
                position,
                gt21_probabilities,
                genotype_probabilities,
                variant_length_probabilities_1,
                variant_length_probabilities_2,
                "Normal output" if not is_reference else "Reference"
            )
        else:
            output_utilities.output("%s\t%d\t.\t%s\t%s\t%d\t%s\t%s\tGT:GQ:DP:AF\t%s:%d:%d:%.4f" % (
                chromosome,
                position,
                reference_base,
                alternate_base,
                quality_score,
                filtration_value,
                information_string,
                genotype_string,
                quality_score,
                read_depth,
                allele_frequency
            ))


def log_activation(args, m):
    if args.log_path is None:
        return

    summary_writer = m.get_summary_file_writer(args.log_path)
    if summary_writer is None:
        return

    tensor_generator = utils.tensor_generator_from(args.tensor_fn, param.predictBatchSize)
    logging.info("Plotting activations ...")

    num_plotted = 0
    while num_plotted < args.max_plot or args.max_plot < 0:
        print("Getting next batch")
        try:
            batch_X, batch_chr_pos_seq = next(tensor_generator)
        except StopIteration:
            break
        batch_size = len(batch_chr_pos_seq)
        print("Batch generation complete %d" % batch_size)
        # strip away the reference string, keeping the chr and coor only
        batch_chr_pos_seq = [chr+":"+pos for chr, pos, _ in batch_chr_pos_seq]
        summaries = m.get_activation_summary(
            batch_X,
            operations=m.layers,
            batch_item_suffixes=batch_chr_pos_seq,
            max_plot_in_batch=args.max_plot - num_plotted if args.max_plot >= 0 else batch_size,
            parallel_level=args.parallel_level,
            num_workers=args.workers,
            fast_plotting=args.fast_plotting
        )
        for summary in summaries:
            summary_writer.add_summary(summary)
        num_plotted += min(batch_size, args.max_plot - num_plotted if args.max_plot >= 0 else batch_size)
    print("Finished plotting %d" % num_plotted)


def call_variants(args, m):
    output_config = OutputConfig(
        is_show_reference=args.showRef,
        is_debug=args.debug,
        quality_score_for_pass=args.qual,
    )
    output_utilities = output_utilties_from(
        sample_name=args.sampleName,
        is_debug=args.debug,
        is_using_pysam_for_all_indel_bases_output=args.pysam_for_all_indel_bases,
        reference_file_path=args.ref_fn,
        bam_file_path=args.bam_fn,
        output_file_path=args.call_fn,
    )

    output_utilities.output_header()

    tensor_generator = utils.tensor_generator_from(args.tensor_fn, param.predictBatchSize)
    logging.info("Calling variants ...")
    variant_call_start_time = time.time()

    is_finish_loaded_all_mini_batches = False
    mini_batches_loaded = []
    mini_batches_to_predict = []
    mini_batches_to_output = []

    def load_mini_batch():
        try:
            mini_batches_loaded.append(next(tensor_generator))
        except StopIteration:
            return

    while True:
        thread_pool = []

        if len(mini_batches_to_output) > 0:
            mini_batch = mini_batches_to_output.pop(0)
            gt21, zygosity, variant_length_1, variant_length_2 = (
                m.predictBaseRTVal, m.predictGenotypeRTVal, m.predictIndelLengthRTVal1, m.predictIndelLengthRTVal2
            )
            thread_pool.append(Thread(
                target=batch_output,
                args=(
                    mini_batch,
                    gt21, zygosity, variant_length_1, variant_length_2,
                    output_config, output_utilities,
                )
            ))

        if len(mini_batches_to_predict) > 0:
            mini_batch = mini_batches_to_predict.pop(0)
            X, _ = mini_batch
            thread_pool.append(Thread(target=m.predict, args=(X, True)))
            mini_batches_to_output.append(mini_batch)

        if not is_finish_loaded_all_mini_batches:
            thread_pool.append(Thread(target=load_mini_batch))

        for t in thread_pool:
            t.start()
        for t in thread_pool:
            t.join()

        is_finish_loaded_all_mini_batches = len(mini_batches_loaded) == 0
        while len(mini_batches_loaded) > 0:
            mini_batch = mini_batches_loaded.pop(0)
            mini_batches_to_predict.append(mini_batch)

        is_nothing_to_predict_and_output = (
            len(thread_pool) <= 0 and len(mini_batches_to_predict) <= 0 and len(mini_batches_to_output) <= 0
        )
        if is_finish_loaded_all_mini_batches and is_nothing_to_predict_and_output:
            break

    logging.info("Total time elapsed: %.2f s" % (time.time() - variant_call_start_time))

    output_utilities.close_opened_files()


def main():
    parser = argparse.ArgumentParser(
        description="Call variants using a trained Clair model and tensors of candididate variants")

    parser.add_argument('--tensor_fn', type=str, default="PIPE",
                        help="Tensor input, use PIPE for standard input")

    parser.add_argument('--chkpnt_fn', type=str, default=None,
                        help="Input a checkpoint for testing or continue training")

    parser.add_argument('--call_fn', type=str, default=None,
                        help="Output variant predictions")

    parser.add_argument('--bam_fn', type=str, default="bam.bam",
                        help="BAM file input, default: %(default)s")

    parser.add_argument('--qual', type=int, default=None,
                        help="If set, variant with equal or higher quality will be marked PASS, or LowQual otherwise, optional")

    parser.add_argument('--sampleName', type=str, default="SAMPLE",
                        help="Define the sample name to be shown in the VCF file")

    parser.add_argument('--showRef', action='store_true',
                        help="Show reference calls, optional")

    parser.add_argument('--debug', action='store_true',
                        help="Debug mode, optional")

    parser.add_argument('--ref_fn', type=str, default=None,
                        help="Reference fasta file input, optional, print contig tags in the VCF header if set")

    parser.add_argument('--threads', type=int, default=None,
                        help="Number of threads, optional")

    parser.add_argument('--activation_only', action='store_true',
                        help="Output activation only, no prediction")
    parser.add_argument('--max_plot', type=int, default=10,
                        help="The maximum number of plots output, negative number means no limit (plot all), default: %(default)s")
    parser.add_argument('--log_path', type=str, nargs='?', default=None,
                        help="The path for tensorflow logging, default: %(default)s")
    parser.add_argument('-p', '--parallel_level', type=int, default=2,
                        help="The level of parallelism in plotting (currently available: 0, 2), default: %(default)s")
    parser.add_argument('--fast_plotting', action='store_true',
                        help="Enable fast plotting.")
    parser.add_argument('-w', '--workers', type=int, default=8,
                        help="The number of workers in plotting, default: %(default)s")

    parser.add_argument('--pysam_for_all_indel_bases', action='store_true',
                        help="Always using pysam for outputting indel bases, optional")

    args = parser.parse_args()

    if len(sys.argv[1:]) == 0:
        parser.print_help()
        sys.exit(1)

    Run(args)


if __name__ == "__main__":
    main()
