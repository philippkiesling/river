from abc import ABCMeta, abstractmethod
import math

from creme.drift import ADWIN
from creme.tree._tree_utils import do_naive_bayes_prediction
from creme.utils.skmultiflow_utils import check_random_state, normalize_values_in_dict

from .base import FoundNode
from .base import SplitNode
from .base import ActiveLeaf, InactiveLeaf
from .htc_nodes import ActiveLearningNodeNBA

# TODO: check whether or not children can be None, after get_child. Perhaps checks such as
# 'if child is not None' can be safely removed


class AdaNode(metaclass=ABCMeta):
    """ Abstract Class to create a New Node for the Hoeffding Adaptive Tree classifier """

    @property
    @abstractmethod
    def n_leaves(self):
        pass

    @property
    @abstractmethod
    def error_estimation(self):
        pass

    @property
    @abstractmethod
    def error_width(self):
        pass

    @abstractmethod
    def error_is_null(self):
        pass

    @abstractmethod
    def kill_tree_children(self, hat):
        pass

    @abstractmethod
    def learn_one(self, x, y, sample_weight, tree, parent, parent_branch):
        pass

    @abstractmethod
    def filter_instance_to_leaves(self, x, parent, parent_branch, found_nodes):
        pass


class AdaLearningNode(ActiveLearningNodeNBA, AdaNode):
    """ Learning node for Hoeffding Adaptive Tree.

    Uses Adaptive Naive Bayes models.

    Parameters
    ----------
    initial_stats
        Initial class observations.
    depth
        The depth of the learning node in the tree.
    adwin_delta
        The delta parameter of ADWIN.
    random_state
        Seed to control the generation of random numbers and support reproducibility.

    """
    def __init__(self, initial_stats, depth, adwin_delta, random_state):
        super().__init__(initial_stats, depth)
        self.adwin_delta = adwin_delta
        self._adwin = ADWIN(delta=self.adwin_delta)
        self.error_change = False
        self._random_state = check_random_state(random_state)

    @property
    def n_leaves(self):
        return 1

    @property
    def error_estimation(self):
        return self._adwin.estimation

    @property
    def error_width(self):
        return self._adwin.width

    def error_is_null(self):
        return self._adwin is None

    def kill_tree_children(self, hat):
        pass

    def learn_one(self, x, y, sample_weight, tree, parent, parent_branch):
        true_class = y

        if tree.bootstrap_sampling:
            # Perform bootstrap-sampling
            k = self._random_state.poisson(1.0)
            if k > 0:
                sample_weight = sample_weight * k

        aux = self.predict_one(x, tree=tree)
        class_prediction = max(aux, key=aux.get) if aux else None

        is_correct = (true_class == class_prediction)

        if self._adwin is None:
            self._adwin = ADWIN(delta=self.adwin_delta)

        old_error = self.error_estimation

        # Update ADWIN
        self.error_change, _ = self._adwin.update(0.0 if is_correct else 1.0)

        # Error is decreasing
        if self.error_change and old_error > self.error_estimation:
            self.error_change = False

        # Update statistics
        super().learn_one(x, y, sample_weight=sample_weight, tree=tree)

        weight_seen = self.total_weight

        if weight_seen - self.last_split_attempt_at >= tree.grace_period:
            if self.depth >= tree.max_depth:
                # Depth-based pre-pruning
                tree._deactivate_leaf(self, parent, parent_branch)
            else:
                tree._attempt_to_split(self, parent, parent_branch)
                self.last_split_attempt_at = weight_seen

    # Override LearningNodeNBAdaptive
    def predict_one(self, x, *, tree=None):
        if not self.stats:
            return

        prediction_option = tree.leaf_prediction
        # MC
        dist = self.stats
        # NB
        if prediction_option == tree._NAIVE_BAYES:
            if self.total_weight >= tree.nb_threshold:
                dist = do_naive_bayes_prediction(x, self.stats, self.attribute_observers)
        elif prediction_option == tree._NAIVE_BAYES_ADAPTIVE:
            dist = super().predict_one(x, tree=tree)

        dist_sum = sum(dist.values())
        normalization_factor = dist_sum * self.error_estimation * self.error_estimation

        # Weight node's responses accordingly to the estimated error monitored by ADWIN
        # Useful if both the predictions of the alternate tree and the ones from the main tree
        # are combined -> give preference to the most accurate one
        if normalization_factor > 0.0:
            dist = normalize_values_in_dict(dist, normalization_factor, inplace=False)

        return dist

    # Override AdaNode: enable option vote (query potentially more than one leaf for responses)
    def filter_instance_to_leaves(self, X, parent, parent_branch, found_nodes):
        found_nodes.append(FoundNode(self, parent, parent_branch))


class AdaSplitNode(SplitNode, AdaNode):
    """ Node that splits the data in a Hoeffding Adaptive Tree.

    Parameters
    ----------
    split_test
        Split test.
    stats
        Class observations
    depth
        The depth of the node.
    adwin_delta
        The delta parameter of ADWIN.
    random_state
        Internal random state used to sample from poisson distributions.
    """
    def __init__(self, split_test, stats, depth, adwin_delta, random_state):
        super().__init__(split_test, stats, depth)
        self.adwin_delta = adwin_delta
        self._adwin = ADWIN(delta=self.adwin_delta)
        self._alternate_tree = None
        self._error_change = False

        self._random_state = check_random_state(random_state)

    @property
    def n_leaves(self):
        num_of_leaves = 0
        for child in self._children.values():
            if child is not None:
                num_of_leaves += child.n_leaves

        return num_of_leaves

    @property
    def error_estimation(self):
        return self._adwin.estimation

    @property
    def error_width(self):
        w = 0.0
        if not self.error_is_null():
            w = self._adwin.width

        return w

    def error_is_null(self):
        return self._adwin is None

    def learn_one(self, x, y, sample_weight, tree, parent, parent_branch):
        true_class = y
        class_prediction = 0

        leaf = self.filter_instance_to_leaf(x, parent, parent_branch)
        if leaf.node is not None:
            aux = leaf.node.predict_one(x, tree=tree)
            class_prediction = max(aux, key=aux.get)

        is_correct = (true_class == class_prediction)

        # Update stats as traverse the tree to improve predictions (in case split nodes are used
        # to provide responses)
        try:
            self.stats[y] += sample_weight
        except KeyError:
            self.stats[y] = sample_weight

        if self._adwin is None:
            self._adwin = ADWIN(self.adwin_delta)

        old_error = self.error_estimation

        # Update ADWIN
        self._error_change, _ = self._adwin.update(0.0 if is_correct else 1.0)

        # Classification error is decreasing: skip drift adaptation
        if self._error_change and old_error > self.error_estimation:
            self._error_change = False

        # Check condition to build a new alternate tree
        if self._error_change:
            self._alternate_tree = tree._new_learning_node(parent=self)
            self._alternate_tree.depth -= 1  # To ensure we not skip a tree level
            tree._n_alternate_trees += 1

        # Condition to replace alternate tree
        elif self._alternate_tree is not None and not self._alternate_tree.error_is_null():
            if self.error_width > tree.drift_window_threshold \
                    and self._alternate_tree.error_width > tree.drift_window_threshold:
                old_error_rate = self.error_estimation
                alt_error_rate = self._alternate_tree.error_estimation
                fDelta = .05
                fN = 1.0 / self._alternate_tree.error_width + 1.0 / self.error_width

                bound = math.sqrt(2.0 * old_error_rate * (1.0 - old_error_rate) *
                                  math.log(2.0 / fDelta) * fN)
                if bound < (old_error_rate - alt_error_rate):
                    tree._n_active_leaves -= self.n_leaves
                    tree._n_active_leaves += self._alternate_tree.n_leaves
                    self.kill_tree_children(tree)

                    if parent is not None:
                        parent.set_child(parent_branch, self._alternate_tree)
                    else:
                        # Switch tree root
                        tree._tree_root = tree._tree_root._alternate_tree
                    tree._n_switch_alternate_trees += 1
                elif bound < alt_error_rate - old_error_rate:
                    if isinstance(self._alternate_tree, SplitNode):
                        self._alternate_tree.kill_tree_children(tree)
                    self._alternate_tree = None
                    tree._n_pruned_alternate_trees += 1

        # Learn one sample in alternate tree and child nodes
        if self._alternate_tree is not None:
            self._alternate_tree.learn_one(x, y, sample_weight, tree, parent, parent_branch)
        child_branch = self.instance_child_index(x)
        child = self.get_child(child_branch)
        if child is not None:
            try:
                child.learn_one(x, y, sample_weight=sample_weight, tree=tree, parent=self,
                                parent_branch=child_branch)
            except TypeError:  # inactive node
                child.learn_one(x, y, sample_weight=sample_weight, tree=tree)
        # Instance contains a categorical value previously unseen by the split node
        elif self.split_test.branch_for_instance(x) < 0:
            # Creates a new learning node to encompass the new observed feature
            # value
            leaf_node = tree._new_learning_node(parent=self)
            branch_id = self.split_test.add_new_branch(
                x[self.split_test.get_atts_test_depends_on()[0]])
            self.set_child(branch_id, leaf_node)
            tree._n_active_leaves += 1
            leaf_node.learn_one(x, y, sample_weight, tree, parent, parent_branch)

    def predict_one(self, X, *, tree=None):
        # In case split nodes end up being used (if emerging categorical feature appears,
        # for instance)
        return self.stats  # Use the MC (majority class) prediction strategy

    # Override AdaNode
    def kill_tree_children(self, tree):
        for child_id, child in self._children.items():
            if child is not None:
                # Delete alternate tree if it exists
                if isinstance(child, SplitNode):
                    if child._alternate_tree is not None:
                        child._alternate_tree.kill_tree_children(tree)
                        tree._n_pruned_alternate_trees += 1

                    # Recursive delete of SplitNodes
                    child.kill_tree_children(tree)
                    self._n_decision_nodes -= 1

                if isinstance(child, ActiveLeaf):
                    tree._n_active_leaves -= 1
                elif isinstance(child, InactiveLeaf):
                    tree._n_inactive_leaves -= 1

                self._children[child_id] = None

    # override AdaNode
    def filter_instance_to_leaves(self, x, parent, parent_branch, found_nodes):
        child_index = self.instance_child_index(x)
        if child_index >= 0:
            child = self.get_child(child_index)
            if child is not None:
                try:
                    child.filter_instance_to_leaves(x, parent, parent_branch, found_nodes)
                except AttributeError:  # inactive leaf
                    found_nodes.append(child.filter_instance_to_leaf(x, parent, parent_branch))
            else:
                found_nodes.append(FoundNode(None, self, child_index))
        if self._alternate_tree is not None:
            self._alternate_tree.filter_instance_to_leaves(x, self, -999, found_nodes)
