import os
import networkx as nx
import csv
import math

from srearena.conductor.oracles.base import Oracle
from srearena.conductor.oracles.utils import is_exact_match, is_subset
from srearena.observer import root_path

class LocalizationOracle(Oracle):
    def __init__(self, problem, expected: list[str]):
        super().__init__(problem)
        self.expected = expected

    def _fetch_topology_map(self):
        file_path = root_path / "topology_graph" / f"{self.problem.namespace}.csv"
        if not os.path.exists(file_path):
            print(f"No snapshot of the topology of {self.problem.namespace} currently exists.")

        self.topology_graph = nx.Graph()

        with open(file_path, "r", newline="") as f:
            reader = csv.reader(f)
            next(reader)
            for row in reader:
                if len(row) >= 2:
                    u, v = row[0], row[1]
                    self.topology_graph.add_edge(u, v, weight=1)
    
    def ntam_score(self, predicted_nodes, ground_truths, alpha=1.0, beta=1.0, gamma=1.0, omega=0.7, tau=0.8):
        """
        Compute Normalized Topology-Aware Match (NTAM) score.

        Parameters:
            predicted_nodes - list of predicted entities (nodes)
            ground_truths   - list of ground truth entities (nodes)
            alpha           - node importance weight
            beta            - subtree size weight
            gamma           - length mismatch penalty weight
            omega           - shrink rate for importance factor
            tau             - shrink rate for topology distance
        """
        dist = dict(nx.all_pairs_dijkstra_path_length(self.topology_graph, weight="weight"))

        score = 0.0
        for i, pred_node in enumerate(predicted_nodes):
            node_importance_factor = omega ** i
            if pred_node in ground_truths:
                score += alpha * node_importance_factor
            else:
                dists = []
                for ground_truth in ground_truths:
                    if pred_node in dist and ground_truth in dist[pred_node]:
                        dists.append(dist[pred_node][ground_truth])
                    if ground_truth in dist and pred_node in dist[ground_truth]:
                        dists.append(dist[ground_truth][pred_node])
                if dists:
                    # Only need to match the predicted node to the nearest ground truth node
                    min_dist = min(dists)
                    subtree_factor = (math.log((self.topology_graph.size() + 1) / (self.topology_graph.degree(pred_node) + 1)) + 1) / (math.log(self.topology_graph.size() + 1) + 1)
                    score += alpha * subtree_factor * (tau ** min_dist)
        
        max_possible = 0.0
        for i, _ in enumerate(ground_truths):
            node_importance_factor = omega ** i
            max_possible += alpha * node_importance_factor

        score = min(1, score / max_possible)

        length_mismatch_penalty = math.exp(-gamma * abs(len(predicted_nodes) - len(ground_truths)))
        # Apply penalty after normalizing
        score *= length_mismatch_penalty

        return score

    def evaluate(self, solution) -> dict:
        print("== Localization Evaluation ==")
        results = {}

        self._fetch_topology_map()

        # Normalize string input to list
        if isinstance(solution, str):
            solution = [solution]
        elif not isinstance(solution, list):
            results["accuracy"] = 0.0
            results["success"] = False
            print("❌ Invalid format: expected string or list of strings")
            return results

        # Safety check: ensure all items are strings
        if not all(isinstance(item, str) for item in solution):
            results["accuracy"] = 0.0
            results["success"] = False
            print("❌ Invalid content: all items must be strings")
            return results

        ntam_score = self.ntam_score(solution, self.expected)

        if ntam_score == 1.0:
            print(f"✅ {solution}: Exact match")
        elif ntam_score > 0:
            print(f"⚠️ {solution}: Partially match | NTAM Score: {ntam_score:.2f}")
        else:
            print(f"❌ {solution}: No match")

        results["accuracy (NTAM)"] = ntam_score
        results["success"] = ntam_score == 1.0
        return results

