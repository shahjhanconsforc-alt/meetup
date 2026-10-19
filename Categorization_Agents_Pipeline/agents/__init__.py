"""The eight agents, in the order a profile passes through them.

    DataLoaderAgent     member store -> [Profile]
    BioCleaningAgent    Profile -> CleanBio
    InterestAgent       CleanBio -> [Signal]   (category terms)
    EntityAgent         CleanBio -> [Signal]   (named things) + inference
    ConfidenceAgent     [Signal] -> {subject: score} -> what survives
    ClassifierAgent     signals + scores -> [category]
    TaggerAgent         signals + categories -> [tag]
    WriterAgent         Verdict -> category store

Each is a class with one public method and no state that changes per profile, so
the middle six can be shared by every worker without a lock. The two that DO have
state — the loader's report and the writer's buffer — are the two that touch
MinIO, and they are deliberately the only ones at the edges of the chain.
"""
from .classifier import ClassifierAgent
from .cleaner import BioCleaningAgent
from .confidence import ConfidenceAgent
from .entities import EntityAgent
from .interests import InterestAgent
from .loader import DataLoaderAgent
from .tagger import TaggerAgent
from .writer import WriterAgent

__all__ = [
    "BioCleaningAgent",
    "ClassifierAgent",
    "ConfidenceAgent",
    "DataLoaderAgent",
    "EntityAgent",
    "InterestAgent",
    "TaggerAgent",
    "WriterAgent",
]
