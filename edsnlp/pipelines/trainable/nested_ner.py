from collections import defaultdict
from itertools import islice
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Tuple

import numpy as np
import spacy
from spacy import Language
from spacy.pipeline.trainable_pipe import TrainablePipe
from spacy.tokens import Doc, Span
from spacy.training.example import Example
from spacy.vocab import Vocab
from thinc.api import Model, Optimizer
from thinc.config import Config
from thinc.model import set_dropout_rate
from thinc.types import Ints2d
from wasabi import Printer

from edsnlp.utils.filter import filter_spans

msg = Printer()

glob = {}

nested_ner_default_config = """
[model]
    @architectures = "eds.nested_ner_model.v1"
    mode = "joint"

    [model.tok2vec]
        @architectures = "spacy.Tok2Vec.v1"

    [model.tok2vec.embed]
        @architectures = "spacy.MultiHashEmbed.v1"
        width = 96
        rows = [5000, 2000, 1000, 1000]
        attrs = ["ORTH", "PREFIX", "SUFFIX", "SHAPE"]
        include_static_vectors = false

    [model.tok2vec.encode]
        @architectures = "spacy.MaxoutWindowEncoder.v1"
        width = ${model.tok2vec.embed.width}
        window_size = 1
        maxout_pieces = 3
        depth = 4

[scorer]
    @scorers = "eds.nested_ner_scorer.v1"
"""

NESTED_NER_DEFAULTS = Config().from_str(nested_ner_default_config)


@Language.factory(
    "nested_ner",
    default_config=NESTED_NER_DEFAULTS,
    requires=["doc.ents", "doc.spans"],
    assigns=["doc.ents", "doc.spans"],
    default_score_weights={
        "ents_f": 1.0,
        "ents_p": 0.0,
        "ents_r": 0.0,
    },
)
def create_component(
    nlp: Language,
    name: str,
    model: Model,
    ent_labels=None,
    spans_labels=None,
    scorer=None,
):
    """Construct a TrainableQualifier component."""
    return TrainableNer(
        vocab=nlp.vocab,
        model=model,
        name=name,
        ent_labels=ent_labels,
        spans_labels=spans_labels,
        scorer=scorer,
    )


def nested_ner_scorer(examples: Iterable[Example], **cfg):
    """
    Scores the extracted entities that may be overlapping or nested
    by looking in `doc.ents`, and `doc.spans`.

    Parameters
    ----------
    examples: Iterable[Example]
    cfg: Dict[str]
        - labels: Iterable[str] labels to take into account
        - spans_labels: Iterable[str] span group names to look into for entities

    Returns
    -------
    Dict[str, float]
    """
    labels = set(cfg["labels"]) if "labels" in cfg is not None else None
    spans_labels = cfg["spans_labels"]

    pred_spans = set()
    gold_spans = set()
    for eg_idx, eg in enumerate(examples):
        for span in (
            *eg.predicted.ents,
            *(
                span
                for name in (
                    spans_labels if spans_labels is not None else eg.reference.spans
                )
                for span in eg.predicted.spans.get(name, ())
            ),
        ):
            if labels is None or span.label_ in labels:
                pred_spans.add((eg_idx, span.start, span.end, span.label_))

        for span in (
            *eg.reference.ents,
            *(
                span
                for name in (
                    spans_labels if spans_labels is not None else eg.reference.spans
                )
                for span in eg.reference.spans.get(name, ())
            ),
        ):
            if labels is None or span.label_ in labels:
                gold_spans.add((eg_idx, span.start, span.end, span.label_))

    tp = len(pred_spans & gold_spans)

    return {
        "ents_p": tp / len(pred_spans) if pred_spans else float(tp == len(pred_spans)),
        "ents_r": tp / len(gold_spans) if gold_spans else float(tp == len(gold_spans)),
        "ents_f": 2 * tp / (len(pred_spans) + len(gold_spans))
        if pred_spans or gold_spans
        else float(len(pred_spans) == len(gold_spans)),
    }


@spacy.registry.scorers("eds.nested_ner_scorer.v1")
def make_nested_ner_scorer():
    return nested_ner_scorer


# noinspection PyMethodOverriding
class TrainableNer(TrainablePipe):
    def __init__(
        self,
        vocab: Vocab,
        model: Model,
        name: str = "nested_ner",
        ent_labels: Iterable[str] = (),
        spans_labels: Mapping[str, Iterable[str]] = None,
        scorer: Optional[Callable] = None,
    ) -> None:
        """
        Initialize a general named entity recognizer (with or without nested or
        overlapping entities).

        Parameters
        ----------
        vocab: Vocab
            Spacy vocabulary
        model: Model
            Model to extract these entities
        name: str
            Name of the component
        ent_labels: Iterable[str]
            List of labels to filter entities for in `doc.ents`
        spans_labels: Mapping[str, Iterable[str]]
            Mapping from span group names to list of labels to look for entities
            and assign the predicted entities
        scorer: Optional[Callable]
            Method to call to score predictions
        """

        super().__init__(vocab, model, name)

        self.cfg["ent_labels"]: Optional[Tuple[str]] = (
            tuple(ent_labels) if ent_labels is not None else None
        )
        self.cfg["spans_labels"]: Optional[Dict[str, Tuple[str]]] = (
            {k: tuple(labels) for k, labels in spans_labels.items()}
            if spans_labels is not None
            else None
        )
        self.cfg["labels"] = tuple(
            sorted(
                set(
                    (list(ent_labels) if ent_labels is not None else [])
                    + [
                        label
                        for group in (spans_labels or {}).values()
                        for label in group
                    ]
                )
            )
        )

        self.scorer = scorer

    @property
    def labels(self) -> Tuple[str]:
        """Return the labels currently added to the component."""
        return self.cfg["labels"]

    @property
    def spans_labels(self) -> Dict[str, Tuple[str]]:
        """Return the span group to labels filters mapping"""
        return self.cfg["spans_labels"]

    @property
    def ent_labels(self):
        """Return the doc.ents labels filters"""
        return self.cfg["ent_labels"]

    def add_label(self, label: str) -> int:
        """Add a new label to the pipe."""
        raise Exception("Cannot add a new label to the pipe")

    def predict(self, docs: List[Doc]) -> Ints2d:
        """Apply the pipeline's model to a batch of docs, without modifying them."""
        return self.model.predict((docs, None, True))[1]

    def set_annotations(self, docs: List[Doc], predictions: Ints2d, **kwargs) -> None:
        """Modify a batch of `Doc` objects, using predicted spans."""
        docs = list(docs)
        new_doc_spans: List[List[Span]] = [[] for _ in docs]
        for doc_idx, label_idx, begin, end in predictions:
            label = self.labels[label_idx]
            new_doc_spans[doc_idx].append(Span(docs[doc_idx], begin, end, label))

        for doc, new_spans in zip(docs, new_doc_spans):
            # Only add a span to `doc.ents` if its label is in `self.ents_labels`
            doc.ents = filter_spans(
                [s for s in new_spans if s.label_ in self.ent_labels]
            )

            # Only add a span to `doc.spans[name]` if its label is in the matching
            # `self.spans_labels[name]` list
            for name, group_labels in self.spans_labels.items():
                doc.spans[name] = [s for s in new_spans if s.label_ in group_labels]

    def update(
        self,
        examples: Iterable[Example],
        *,
        drop: float = 0.0,
        set_annotations: bool = False,
        sgd: Optional[Optimizer] = None,
        losses: Optional[Dict[str, float]] = None,
    ) -> Dict[str, float]:
        """Learn from a batch of documents and gold-standard information,
        updating the pipe's model. Delegates to predict and get_loss."""

        glob["last_examples"] = examples

        if losses is None:
            losses = {}
        losses.setdefault(self.name, 0.0)
        set_dropout_rate(self.model, drop)
        examples = list(examples)

        # run the model
        docs = [eg.predicted for eg in examples]
        gold = self.examples_to_truth(examples)
        (loss, predictions), backprop = self.model.begin_update(
            (docs, gold, set_annotations)
        )
        loss, gradient = self.get_loss(examples, loss)
        backprop(gradient)
        if sgd is not None:
            self.model.finish_update(sgd)
        losses[self.name] += loss
        if set_annotations:
            self.set_annotations(docs, predictions)
        return losses

    def get_loss(self, examples: Iterable[Example], loss) -> Tuple[float, float]:
        """Find the loss and gradient of loss for the batch of documents and
        their predicted scores."""
        return loss, np.full((), fill_value=1)

    def initialize(
        self,
        get_examples: Callable[[], Iterable[Example]],
        *,
        nlp: Language = None,
        labels: Optional[List[str]] = None,
    ):
        """Initialize the pipe for training, using a representative set
        of data examples.
        """
        sub_batch = list(islice(get_examples(), 100))
        if self.ent_labels is None and self.spans_labels is None:
            self.cfg["ent_labels"] = tuple(
                sorted(
                    {span.label_ for doc in sub_batch for span in doc.reference.ents}
                )
            )

            spans_labels = defaultdict(lambda: set())
            for doc in sub_batch:
                for name, group in doc.reference.spans.items():
                    for span in group:
                        spans_labels[name].add(span.label_)

            self.cfg["spans_labels"] = {
                name: tuple(sorted(group)) for name, group in spans_labels.items()
            }

            self.cfg["labels"] = tuple(
                sorted(
                    set(
                        (list(self.ent_labels) if self.ent_labels is not None else [])
                        + [
                            label
                            for group in (spans_labels or {}).values()
                            for label in group
                        ]
                    )
                )
            )

        doc_sample = [eg.reference for eg in sub_batch]
        spans_sample = self.examples_to_truth(sub_batch)
        if spans_sample is None:
            raise ValueError(
                "Call begin_training with relevant entities "
                "and relations annotated in "
                "at least a few reference examples!"
            )
        self.model.initialize(X=doc_sample, Y=spans_sample)
        print("LABELS", self.labels)
        self.model.attrs["set_n_labels"](len(self.labels))

    def examples_to_truth(self, examples: List[Example]) -> Ints2d:
        # check that there are actually any candidate instances
        # in this batch of examples
        label_vocab = {self.vocab.strings[l]: i for i, l in enumerate(self.labels)}
        spans = set()
        for eg_idx, eg in enumerate(examples):
            by_label = {i: [] for i in range(len(label_vocab.items()))}
            for span in (
                *eg.reference.ents,
                *(
                    span
                    for name in (
                        self.spans_labels
                        if self.spans_labels is not None
                        else eg.reference.spans
                    )
                    for span in eg.reference.spans.get(name, ())
                ),
            ):
                label_idx = label_vocab.get(span.label)
                if label_idx is None:
                    continue
                by_label[label_idx].append((span.start, span.end))
                spans.add((eg_idx, label_idx, span.start, span.end))

        truths = self.model.ops.asarray(list(spans))
        return truths