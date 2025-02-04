"""

This module contains model selection logic based on multi-armed bandits (MAB). The way the code is
organised, the bandit logic is agnostic of the model selection aspect.

"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from random import Random
from typing import Iterator, List

from river import base, metrics, utils

from .base import ModelSelectionRegressor


@dataclass
class Arm:
    """An arm in a multi-armed bandit.

    It may also be referred to as a "lever".

    """

    index: int
    metric: metrics.Metric
    n_pulls: int = 0


class Bandit:
    """(Multi-armed) bandit (MAB) policy.

    A bandit is composed of multiple arms. A policy is in charge of determining the best one.

    """

    def __init__(self, n_arms: int, metric: metrics.Metric):
        self.arms = [Arm(index=i, metric=metric.clone()) for i in range(n_arms)]
        self.metric = metric
        self.best_arm = self.arms[0]
        self.n_pulls = 0

    def update(self, arm: Arm, **metric_kwargs):
        self.n_pulls += 1
        arm.n_pulls += 1
        arm.metric.update(**metric_kwargs)
        self.best_arm = max(
            self.arms,
            key=lambda arm: arm.metric.get()
            if self.metric.bigger_is_better
            else -arm.metric.get(),
        )

    @property
    def ranking(self):
        return [
            arm.index
            for arm in sorted(
                self.arms,
                key=lambda arm: arm.metric.get(),
                reverse=self.metric.bigger_is_better,
            )
        ]

    def __repr__(self):
        return utils.pretty.print_table(
            headers=["Ranking", self.metric.__class__.__name__, "Pulls", "Share",],
            columns=[
                [f"#{self.ranking.index(arm.index)}" for arm in self.arms],
                [f"{arm.metric.get():{self.metric._fmt}}" for arm in self.arms],
                [f"{arm.n_pulls:,d}" for arm in self.arms],
                [f"{arm.n_pulls / self.n_pulls:.2%}" for arm in self.arms],
            ],
        )


class BanditPolicy(ABC):
    """A policy for solving bandit problems."""

    def __init__(self, burn_in: int, seed: int):
        self.burn_in = burn_in
        self.seed = seed
        self.rng = Random(seed)

    def pull(self, bandit: Bandit) -> Iterator[Arm]:
        burn_in_over = True
        for arm in bandit.arms:
            if arm.n_pulls < self.burn_in:
                yield arm
                burn_in_over = False
        if burn_in_over:
            yield from self._pull(bandit)

    @abstractmethod
    def _pull(self, bandit: Bandit) -> Iterator[Arm]:
        ...


class BanditRegressor(ModelSelectionRegressor):
    def __init__(
        self,
        models: List[base.Regressor],
        metric: metrics.RegressionMetric,
        policy: BanditPolicy,
    ):
        super().__init__(models, metric)
        self.bandit = Bandit(n_arms=len(models), metric=metric)
        self.policy = policy

    @property
    def best_model(self):
        return self[self.bandit.best_arm.index]

    def learn_one(self, x, y):
        for arm in self.policy.pull(self.bandit):
            model = self[arm.index]
            y_pred = model.predict_one(x)
            self.bandit.update(arm, y_true=y, y_pred=y_pred)
            model.learn_one(x, y)
        return self
