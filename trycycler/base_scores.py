"""
Copyright 2019 Ryan Wick (rrwick@gmail.com)
https://github.com/rrwick/Trycycler

This file is part of Trycycler. Trycycler is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by the Free Software Foundation,
either version 3 of the License, or (at your option) any later version. Trycycler is distributed
in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
details. You should have received a copy of the GNU General Public License along with Trycycler.
If not, see <http://www.gnu.org/licenses/>.
"""

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import re

from .alignment import align_reads_to_seq
from .log import log, section_header, explanation
from .misc import means_of_slices
from . import settings


def get_per_base_scores(seqs, reads, circular, threads, plot_qual, fasta_names):
    section_header('Per-base quality scores')
    explanation('Trycycler now aligns all reads to each sequence and uses the alignments to '
                'create per-base quality scores for the entire sequence.')
    per_base_scores = {}
    for seq_name, seq in seqs.items():
        log(f'Aligning reads to sequence {seq_name}:')
        per_base_scores[seq_name] = \
            get_one_seq_per_base_scores(seq, reads, circular, threads)
        if plot_qual:
            plot_per_base_scores(seq_name, per_base_scores[seq_name], fasta_names)
        log()
    return per_base_scores


def get_one_seq_per_base_scores(seq, reads, circular, threads):
    seq_len = len(seq)

    # For circular sequences, alignments are done to a doubled version of the sequence, to allow
    # for alignments around the junction point. We can then toss out any alignments that reside
    # entirely in the second half.
    if circular:
        ref_seq = seq + seq
        alignments = align_reads_to_seq(reads, ref_seq, threads)
        alignments = [a for a in alignments if a.ref_start < seq_len]
    else:
        ref_seq = seq
        alignments = align_reads_to_seq(reads, ref_seq, threads)

    log(f'  {len(alignments):,} alignments')

    per_base_scores = [0] * len(ref_seq)

    for i, a in enumerate(alignments):
        log(f'\r  calculating alignment scores: {i+1} / {len(alignments)}', end='')
        alignment_scores = get_alignment_scores(a)
        for j, s in enumerate(alignment_scores):
            ref_pos = a.ref_start + j
            per_base_scores[ref_pos] += s
    log()

    # If the sequence was doubled, we now have to undouble it by collapsing the two halves of the
    # per-base scores together.
    if circular:
        non_doubled_per_base_scores = [0] * seq_len
        for i in range(seq_len):
            score_1 = per_base_scores[i]
            score_2 = per_base_scores[i + seq_len]
            if score_1 > score_2:
                non_doubled_per_base_scores[i] = score_1
            else:
                non_doubled_per_base_scores[i] = score_2
        per_base_scores = non_doubled_per_base_scores

    total_score = sum(per_base_scores)
    log(f'  total score = {total_score:,}')
    return per_base_scores


def get_alignment_scores(a):
    expanded_cigar = get_expanded_cigar(a)
    pass_fail = get_pass_fail(expanded_cigar)

    # We need to index into these, which is a bit faster for a Python list than for a Numpy array.
    expanded_cigar = expanded_cigar.tolist()
    pass_fail = pass_fail.tolist()

    # We now make the score for each position of the expanded CIGAR. The score increases with
    # matches and resets to zero at fail regions. This is done in both forward and reverse
    # directions.
    forward_scores = get_cigar_scores_forward(expanded_cigar, pass_fail)
    reverse_scores = get_cigar_scores_reverse(expanded_cigar, pass_fail)

    # To make the final scores, we combine the forward and reverse scores, taking the minimum of
    # each. We also drop any insertion positions, so the scores match up with the corresponding
    # range of the reference sequence.
    return combine_forward_and_reverse_scores(a, forward_scores, reverse_scores, expanded_cigar)


def get_expanded_cigar(a):
    """
    An expanded CIGARs has just the four characters (=, X, I and D) repeated (i.e. no numbers).
    Here I store it as integers (0: =, 1: X, 2: I, 3: D) and return it as a Numpy array
    """
    cigar_parts = re.findall(r'\d+[IDX=]', a.cigar)
    cigar_parts = [(int(c[:-1]), c[-1]) for c in cigar_parts]
    expanded_cigar_size = sum(p[0] for p in cigar_parts)
    expanded_cigar = [0] * expanded_cigar_size

    i = 0
    for num, letter in cigar_parts:
        if letter == '=':
            v = 0
        elif letter == 'X':
            v = 1
        elif letter == 'I':
            v = 2
        elif letter == 'D':
            v = 3
        else:
            assert False
        for _ in range(num):
            expanded_cigar[i] = v
            i += 1
    assert i == expanded_cigar_size

    return np.asarray(expanded_cigar, dtype=int)


def get_pass_fail(expanded_cigar):
    # The simplified expanded CIGAR has only two values: 0 for match, 1 for everything else.
    simplified_expanded_cigar = np.copy(expanded_cigar)
    simplified_expanded_cigar[simplified_expanded_cigar > 1] = 1

    # We then sum the values over a sliding window.
    pass_fail = np.convolve(simplified_expanded_cigar,
                            np.ones(settings.BASE_SCORE_WINDOW, dtype=int), 'same')

    # And then simplify this to a pass/fail array with two values: 0 for pass, 1 for fail.
    pass_fail[pass_fail < settings.BASE_SCORE_THRESHOLD] = 0
    pass_fail[pass_fail >= settings.BASE_SCORE_THRESHOLD] = 1

    return pass_fail


def get_cigar_scores_forward(expanded_cigar, pass_fail):
    scores = [0] * len(expanded_cigar)
    score = 0
    for i in range(len(expanded_cigar)):
        if pass_fail[i] == 1:  # fail
            score = 0
        elif expanded_cigar[i] == 0:  # match
            score += 1
        else:  # anything other than a match
            score -= 1
            if score < 0:
                score = 0
        scores[i] = score
    return scores


def get_cigar_scores_reverse(expanded_cigar, pass_fail):
    scores = [0] * len(expanded_cigar)
    score = 0
    for i in range(len(expanded_cigar) - 1, -1, -1):  # loop through indices backwards
        if pass_fail[i] == 1:  # fail
            score = 0
        elif expanded_cigar[i] == 0:  # match
            score += 1
        else:  # anything other than a match
            score -= 1
            if score < 0:
                score = 0
        scores[i] = score
    return scores


def combine_forward_and_reverse_scores(a, forward_scores, reverse_scores, expanded_cigar):
    """
    The combined scores are the minimums values at each position, excluding insertion positions.
    """
    final_scores = [0] * (a.ref_end - a.ref_start)
    assert len(forward_scores) == len(reverse_scores)
    j = 0  # index in final_scores
    for i, f in enumerate(forward_scores):
        r = reverse_scores[i]
        if expanded_cigar[i] != 2:   # if not an insertion
            if f < r:
                final_scores[j] = f
            else:
                final_scores[j] = r
            j += 1
    assert len(final_scores) == j
    return final_scores


class MyAxes(matplotlib.axes.Axes):
    name = 'MyAxes'

    def drag_pan(self, button, _, x, y):
        matplotlib.axes.Axes.drag_pan(self, button, 'x', x, y)  # pretend key=='x'


matplotlib.projections.register_projection(MyAxes)


def plot_per_base_scores(seq_name, per_base_scores, fasta_names, averaging_window=100):
    max_score = max(per_base_scores)
    positions = list(range(len(per_base_scores)))

    score_means = list(means_of_slices(per_base_scores, averaging_window))
    position_means = list(means_of_slices(positions, averaging_window))

    fig, ax1 = plt.subplots(1, 1, figsize=(12, 3), subplot_kw={'projection': 'MyAxes'})
    ax1.plot(position_means, score_means, '-', color='#8F0505')

    plt.xlabel('contig position')
    plt.ylabel('quality score')
    plt.title(f'{seq_name} ({fasta_names[seq_name]})')
    ax1.set_xlim([0, len(per_base_scores)])
    ax1.set_ylim([0, max_score])

    fig.canvas.manager.toolbar.pan()
    plt.show()
