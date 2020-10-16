"""
The ``metric_stats`` module provides an abstract class for storing
statistics produced over the course of an experiment, and summarizing them.

Authors:
 * Peter Plantinga 2020
 * Mirco Ravanelli 2020
"""
import torch
import speechbrain.data_io.wer as wer_io
import speechbrain.utils.edit_distance as edit_distance
from joblib import Parallel, delayed
from speechbrain.data_io.data_io import (
    convert_index_to_lab,
    merge_char,
    split_word,
)
from speechbrain.utils.data_utils import undo_padding
from speechbrain.utils.edit_distance import wer_summary


def multiprocess_evaluation(metric, predict, target, lengths=None, n_jobs=30):
    if lengths is not None:
        lengths = (lengths * predict.size(1)).int().cpu()
        predict = [p[:length].cpu() for p, length in zip(predict, lengths)]
        target = [t[:length].cpu() for t, length in zip(target, lengths)]

    scores = Parallel(n_jobs=n_jobs)(
        delayed(metric)(p, t) for p, t in zip(predict, target)
    )
    return scores


class MetricStats:
    """A default class for storing and summarizing arbitrary metrics.

    More complex metrics can be created by sub-classing this class.

    Arguments
    ---------
    metric : function
        The function to use to compute the relevant metric. Should take
        at least two arguments (predictions and targets) and can
        optionally take the relative lengths of either or both arguments.
        Not usually used in sub-classes.
    n_jobs : int
        The number of jobs to use for computing the metric. If this is
        more than one, every sample is processed individually, otherwise
        the whole batch is passed at once.

    Example
    -------
    >>> from speechbrain.nnet.losses import l1_loss
    >>> loss_stats = MetricStats(metric=l1_loss)
    >>> loss_stats.append(
    ...      ids=["utterance1", "utterance2"],
    ...      predictions=torch.tensor([[0.1, 0.2], [0.2, 0.3]]),
    ...      targets=torch.tensor([[0.1, 0.2], [0.1, 0.2]]),
    ...      reduction="batch",
    ... )
    >>> stats = loss_stats.summarize()
    >>> stats['average']
    0.050...
    >>> stats['max_score']
    0.100...
    >>> stats['max_id']
    'utterance2'
    """

    def __init__(self, metric, n_jobs=1):
        self.metric = metric
        self.n_jobs = n_jobs
        self.clear()

    def clear(self):
        """Creates empty container for storage, removing existing stats."""
        self.scores = []
        self.ids = []
        self.summary = {}

    def append(self, ids, *args, **kwargs):
        """Store a particular set of metric scores.

        Arguments
        ---------
        ids : list
            List of ids corresponding to utterances.
        *args, **kwargs
            Arguments to pass to the metric function
        """
        self.ids.extend(ids)

        # Compute metric, in parallel if requested.
        if self.n_jobs > 1:
            if "predict" not in kwargs or "target" not in kwargs:
                raise ValueError(
                    "Must pass 'predict' and 'target' as kwargs if n_jobs > 1"
                )
            scores = multiprocess_evaluation(
                metric=self.metric, n_jobs=self.n_jobs, **kwargs
            )
        else:
            scores = self.metric(*args, **kwargs).detach()

        self.scores.extend(scores)

    def summarize(self, field=None):
        """Summarize the metric scores, returning relevant stats.

        Arguments
        ---------
        field : str
            If provided, only returns selected statistic. If not,
            returns all computed statistics.

        Returns
        -------
        float or dict
            Returns a float if ``field`` is provided, otherwise
            returns a dictionary containing all computed stats.
        """
        min_index = torch.argmin(torch.tensor(self.scores))
        max_index = torch.argmax(torch.tensor(self.scores))
        self.summary = {
            "average": float(sum(self.scores) / len(self.scores)),
            "min_score": float(self.scores[min_index]),
            "min_id": self.ids[min_index],
            "max_score": float(self.scores[max_index]),
            "max_id": self.ids[max_index],
        }

        if field is not None:
            return self.summary[field]
        else:
            return self.summary

    def write_stats(self, filestream, verbose=False):
        """Write all relevant statistics to file.

        Arguments
        ---------
        filestream : file-like object
            A stream for the stats to be written to.
        verbose : bool
            Whether to also print the stats to stdout.
        """
        if not self.summary:
            self.summarize()

        message = f"Average score: {self.summary['average']}\n"
        message += f"Min error: {self.summary['min_score']} "
        message += f"id: {self.summary['min_id']}\n"
        message += f"Max error: {self.summary['max_score']} "
        message += f"id: {self.summary['max_id']}\n"

        filestream.write(message)
        if verbose:
            print(message)


class ErrorRateStats(MetricStats):
    """A class for tracking error rates (e.g. WER, PER).

    Arguments
    ---------
    merge_tokens : bool
        Whether to merge the successive tokens (used for e.g.
        creating words out of character tokens).

    Example
    -------
    >>> cer_stats = ErrorRateStats()
    >>> cer_stats.append(
    ...     ids=['utterance1'],
    ...     predict=torch.tensor([[0, 1, 1]]),
    ...     target=torch.tensor([[0, 1, 0]]),
    ...     target_len=torch.ones(1),
    ...     ind2lab={0: 'a', 1: 'b'},
    ... )
    >>> stats = cer_stats.summarize()
    >>> stats['WER']
    33.33...
    >>> stats['insertions']
    0
    >>> stats['deletions']
    0
    >>> stats['substitutions']
    1
    """

    def __init__(self, merge_tokens=False, split_tokens=False):
        self.clear()
        self.merge_tokens = merge_tokens
        self.split_tokens = split_tokens

    def append(
        self,
        ids,
        predict,
        target,
        predict_len=None,
        target_len=None,
        ind2lab=None,
    ):
        """Add stats to the relevant containers.

        * See MetricStats.append()

        Arguments
        ---------
        ids : list
            List of ids corresponding to utterances.
        predict : torch.tensor
            A predicted output, for comparison with the target output
        target : torch.tensor
            The correct reference output, for comparison with the prediction.
        predict_len : torch.tensor
            The predictions relative lengths, used to undo padding if
            there is padding present in the predictions.
        target_len : torch.tensor
            The target outputs' relative lengths, used to undo padding if
            there is padding present in the target.
        ind2lab : dict
            Mapping from indices to labels, for writing alignments.
        """
        self.ids.extend(ids)

        if predict_len is not None:
            predict = undo_padding(predict, predict_len)

        if target_len is not None:
            target = undo_padding(target, target_len)

        if ind2lab is not None:
            predict = convert_index_to_lab(predict, ind2lab)
            target = convert_index_to_lab(target, ind2lab)

        if self.merge_tokens:
            predict = merge_char(predict)
            target = merge_char(target)

        if self.split_tokens:
            predict = split_word(predict)
            target = split_word(target)

        scores = edit_distance.wer_details_for_batch(ids, target, predict, True)

        self.scores.extend(scores)

    def summarize(self, field=None):
        """Summarize the error_rate and return relevant statistics.

        * See MetricStats.summarize()
        """
        self.summary = wer_summary(self.scores)

        # Add additional, more generic key
        self.summary["error_rate"] = self.summary["WER"]

        if field is not None:
            return self.summary[field]
        else:
            return self.summary

    def write_stats(self, filestream):
        """Write all relevant info (e.g. error rate alignments) to file.

        * See MetricStats.write_stats()
        """
        if not self.summary:
            self.summarize()

        wer_io.print_wer_summary(self.summary, filestream)
        wer_io.print_alignments(self.scores, filestream)


class BinaryMetricStats(MetricStats):
    """Tracks binary metrics, such as precision, recall, F1, EER, etc.

    """

    def __init__(self, positive_label=1):
        self.clear()
        self.positive_label = positive_label

    def clear(self):
        self.ids = []
        self.scores = []
        self.labels = []
        self.summary = {}

    def append(self, ids, scores, labels):
        """Appends scores and labels to internal lists.

        Does not compute metrics until time of summary, since
        automatic thresholds (e.g. EER) need full set of scores.

        Arguments
        ---------
        ids : list
            The string ids for the samples

        """
        self.ids.extend(ids)
        self.scores.extend(scores.detach())
        self.labels.extend(labels.detach())

    def summarize(self, field=None, threshold=None, beta=1, eps=1e-8):
        """Compute statistics using full set of scores.

        Full set of fields:
         - TP - True Positive
         - TN - True Negative
         - FP - False Positive
         - FN - False Negative
         - FAR - False Acceptance Rate
         - FRR - False Rejection Rate
         - DER - Detection Error Rate (EER if no threshold passed)
         - precision - Precision (positive predictive value)
         - recall - Recall (sensitivity)
         - F-score - Balance of precision and recall (equal if beta=1)
         - MCC - Matthews Correlation Coefficient

        Arguments
        ---------
        field : str
            A key for selecting a single statistic. If not provided,
            a dict with all statistics is returned.
        threshold : float
            If no threshold is provided, equal error rate is used.
        beta : float
            How much to weight precision vs recall in F-score. Default
            of 1. is equal weight, while higher values weight recall
            higher, and lower values weight precision higher.
        eps : float
            A small value to avoid dividing by zero.
        """

        if isinstance(self.scores, list):
            self.scores = torch.stack(self.scores)
            self.labels = torch.stack(self.labels)

        if threshold is None:
            positive_scores = self.scores[self.labels.nonzero(as_tuple=True)]
            negative_scores = self.scores[
                self.labels[self.labels == 0].nonzero(as_tuple=True)
            ]

            threshold = eer_threshold(positive_scores, negative_scores)

        pred = (self.scores >= threshold).float()
        true = self.labels

        TP = self.summary["TP"] = pred.mul(true).sum()
        TN = self.summary["TN"] = (1.0 - pred).mul(1.0 - true).sum()
        FP = self.summary["FP"] = pred.mul(1.0 - true).sum()
        FN = self.summary["FN"] = (1.0 - pred).mul(true).sum()

        self.summary["FAR"] = FP / (TP + TN + eps)
        self.summary["FRR"] = FN / (TP + TN + eps)
        self.summary["DER"] = (FP + FN) / (TP + TN + eps)

        self.summary["precision"] = TP / (TP + FP + eps)
        self.summary["recall"] = TP / (TP + FN + eps)
        self.summary["F-score"] = (
            (1.0 + beta ** 2.0)
            * TP
            / ((1.0 + beta ** 2.0) * TP + beta ** 2.0 * FN + FP)
        )

        self.summary["MCC"] = (TP * TN - FP * FN) / (
            (TP + FP) * (TP + FN) * (TN + FP) * (TN + FN) + eps
        ).sqrt()

        if field is not None:
            return self.summary[field]
        else:
            return self.summary


def eer_threshold(positive_scores, negative_scores):
    """Computes the EER threshold

    Arguments
    ---------
    positive_scores : torch.tensor
        The scores from entiries of the same class.
    negative_scores : torch.tensor
        The scores from entiries of different classes.

    Example
    -------
    >>> postive_scores=torch.tensor([0.6, 0.7, 0.8, 0.5])
    >>> negative_scores=torch.tensor([0.6, 0.4, 0.3, 0.2])
    >>> eer_threshold(postive_scores, negative_scores)
    tensor(0.5500)
    """

    # Computing candidate thresholds
    thresholds, _ = torch.sort(torch.cat([positive_scores, negative_scores]))
    thresholds = torch.unique(thresholds)

    # Adding intermediate thresholds
    interm_thresholds = (thresholds[0:-1] + thresholds[1:]) / 2
    thresholds, _ = torch.sort(torch.cat([thresholds, interm_thresholds]))

    # Computing False Rejection Rate
    positive_scores = torch.cat(
        len(thresholds) * [positive_scores.unsqueeze(0)]
    )
    pos_scores_threshold = positive_scores.transpose(0, 1) <= thresholds
    FRR = (pos_scores_threshold.sum(0)).float() / positive_scores.shape[1]
    del positive_scores
    del pos_scores_threshold

    # Computing False Aceptance Rate
    negative_scores = torch.cat(
        len(thresholds) * [negative_scores.unsqueeze(0)]
    )
    neg_scores_threshold = negative_scores.transpose(0, 1) > thresholds
    FAR = (neg_scores_threshold.sum(0)).float() / negative_scores.shape[1]
    del negative_scores
    del neg_scores_threshold

    # Finding the threshold for EER
    min_index = (FAR - FRR).abs().argmin()
    return thresholds[min_index]