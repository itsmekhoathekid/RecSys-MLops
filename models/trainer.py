import math
from collections import defaultdict

import torch
import torch.nn as nn
from tqdm import tqdm

from .model import BST
from .dataset import Logger


def binary_auc(y_true, y_score):
    """
    Simple AUC implementation without sklearn.
    y_true: list[int] in {0,1}
    y_score: list[float]
    """
    pairs = sorted(zip(y_score, y_true), key=lambda x: x[0])
    n_pos = sum(y_true)
    n_neg = len(y_true) - n_pos
    if n_pos == 0 or n_neg == 0:
        return 0.0

    rank_sum = 0.0
    for rank, (_, label) in enumerate(pairs, start=1):
        if label == 1:
            rank_sum += rank

    auc = (rank_sum - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)
    return float(auc)


def dcg_at_k(relevances, k):
    relevances = relevances[:k]
    return sum(rel / math.log2(idx + 2) for idx, rel in enumerate(relevances))


def ndcg_at_k(labels_sorted_by_pred, k):
    dcg = dcg_at_k(labels_sorted_by_pred, k)
    ideal = sorted(labels_sorted_by_pred, reverse=True)
    idcg = dcg_at_k(ideal, k)
    if idcg == 0:
        return 0.0
    return dcg / idcg


def hitrate_at_k(labels_sorted_by_pred, k):
    return 1.0 if any(labels_sorted_by_pred[:k]) else 0.0


def mrr_at_k(labels_sorted_by_pred, k):
    for idx, rel in enumerate(labels_sorted_by_pred[:k], start=1):
        if rel > 0:
            return 1.0 / idx
    return 0.0


class Trainer:
    def __init__(self, config):
        self.config = config
        self.training_args = config["training_args"]
        self.model_args = config["model_args"]

        self.learning_rate = self.training_args["learning_rate"]
        self.l2_reg = self.training_args["weight_decay"]

        self.logger = Logger()
        self.device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")

        self.model = self.get_model().to(self.device)
        self.optimizer, self.scheduler = self.get_optimizer()
        self.loss_fn = self._get_loss_fn()

        self.ranking_ks = self.training_args.get("ranking_ks", [1, 3, 5, 10])
    
    def get_data_loader(self, dataset, shuffle=False):
        return torch.utils.data.DataLoader(
            dataset,
            batch_size=self.training_args["batch_size"],
            shuffle=shuffle,
            num_workers=self.training_args["num_workers"],
            collate_fn=dataset.collate_fn,
        )

    def _move_batch_to_device(self, batch):
        out = {}
        for k, v in batch.items():
            if torch.is_tensor(v):
                out[k] = v.to(self.device)
            else:
                out[k] = v
        return out

    def _forward_batch(self, batch):
        return self.model(
            hist_item_id=batch["hist_item_id"],
            hist_event_type=batch["hist_event_type"],
            hist_category=batch["hist_category"],
            hist_brand=batch["hist_brand"],
            hist_price_bucket=batch["hist_price_bucket"],
            hist_time=batch["hist_time"],
            target_item_id=batch["target_item_id"],
            target_category=batch["target_category"],
            target_brand=batch["target_brand"],
            target_price_bucket=batch["target_price_bucket"],
        )

    def train(self, train_loader):
        self.model.train()
        total_loss = 0.0
        all_probs = []
        all_labels = []
        all_group_keys = []

        progress_bar = tqdm(train_loader, desc="Training", leave=False)
        for batch in progress_bar:
            batch = self._move_batch_to_device(batch)

            self.optimizer.zero_grad()
            logits = self._forward_batch(batch)
            labels = batch["label"].float()

            loss = self.loss_fn(logits, labels)
            loss.backward()
            self.optimizer.step()

            probs = torch.sigmoid(logits).detach().cpu()
            labels_cpu = labels.detach().cpu()

            total_loss += loss.item()
            all_probs.extend(probs.tolist())
            all_labels.extend(labels_cpu.tolist())

            # group key for ranking metrics:
            # one impression group = (user_id, event_time)
            group_keys = list(
                zip(
                    batch["user_id"].detach().cpu().tolist(),
                    batch["event_time"].detach().cpu().tolist(),
                )
            )
            all_group_keys.extend(group_keys)

            progress_bar.set_postfix({"loss": f"{loss.item():.4f}"})

        metrics = self._compute_metrics(
            all_labels=all_labels,
            all_probs=all_probs,
            all_group_keys=all_group_keys,
        )
        metrics["loss"] = total_loss / max(len(train_loader), 1)
        return metrics

    def evaluate(self, val_loader):
        self.model.eval()
        total_loss = 0.0
        all_probs = []
        all_labels = []
        all_group_keys = []

        progress_bar = tqdm(val_loader, desc="Evaluating", leave=False)
        with torch.no_grad():
            for batch in progress_bar:
                batch = self._move_batch_to_device(batch)

                logits = self._forward_batch(batch)
                labels = batch["label"].float()

                loss = self.loss_fn(logits, labels)
                probs = torch.sigmoid(logits).detach().cpu()
                labels_cpu = labels.detach().cpu()

                total_loss += loss.item()
                all_probs.extend(probs.tolist())
                all_labels.extend(labels_cpu.tolist())

                group_keys = list(
                    zip(
                        batch["user_id"].detach().cpu().tolist(),
                        batch["event_time"].detach().cpu().tolist(),
                    )
                )
                all_group_keys.extend(group_keys)

                progress_bar.set_postfix({"val_loss": f"{loss.item():.4f}"})

        metrics = self._compute_metrics(
            all_labels=all_labels,
            all_probs=all_probs,
            all_group_keys=all_group_keys,
        )
        metrics["loss"] = total_loss / max(len(val_loader), 1)
        return metrics

    def _compute_metrics(self, all_labels, all_probs, all_group_keys):
        metrics = {}

        # flat AUC
        metrics["auc"] = binary_auc(all_labels, all_probs)

        # grouped ranking metrics
        grouped = defaultdict(list)
        for key, label, prob in zip(all_group_keys, all_labels, all_probs):
            grouped[key].append((prob, label))

        valid_groups = 0
        gauc_sum = 0.0

        for k in self.ranking_ks:
            metrics[f"hitrate@{k}"] = 0.0
            metrics[f"ndcg@{k}"] = 0.0
            metrics[f"mrr@{k}"] = 0.0

        for key, pairs in grouped.items():
            labels = [x[1] for x in pairs]
            scores = [x[0] for x in pairs]

            # group AUC only valid when group has both pos and neg
            if len(set(labels)) > 1:
                group_auc = binary_auc(labels, scores)
                gauc_sum += group_auc
                valid_groups += 1

            # sort descending by predicted score
            sorted_pairs = sorted(pairs, key=lambda x: x[0], reverse=True)
            labels_sorted = [x[1] for x in sorted_pairs]

            for k in self.ranking_ks:
                metrics[f"hitrate@{k}"] += hitrate_at_k(labels_sorted, k)
                metrics[f"ndcg@{k}"] += ndcg_at_k(labels_sorted, k)
                metrics[f"mrr@{k}"] += mrr_at_k(labels_sorted, k)

        num_groups = max(len(grouped), 1)
        metrics["gauc"] = gauc_sum / valid_groups if valid_groups > 0 else 0.0

        for k in self.ranking_ks:
            metrics[f"hitrate@{k}"] /= num_groups
            metrics[f"ndcg@{k}"] /= num_groups
            metrics[f"mrr@{k}"] /= num_groups

        metrics["num_groups"] = len(grouped)
        metrics["valid_auc_groups"] = valid_groups
        return metrics

    def get_model(self):
        model = BST(self.model_args)
        if self.model_args.get("reload_model", False):
            model.load_state_dict(torch.load(self.model_args["model_path"], map_location="cpu"))
            print(f"Model loaded from {self.model_args['model_path']}")
        return model

    def get_optimizer(self):
        optimizer = torch.optim.Adam(
            self.model.parameters(),
            lr=self.learning_rate,
            weight_decay=self.l2_reg,
        )

        if self.model_args.get("reload_model", False):
            optimizer_path = self.model_args["model_path"].replace("model", "optimizer")
            scheduler_path = self.model_args["model_path"].replace("model", "scheduler")
            optimizer.load_state_dict(torch.load(optimizer_path, map_location="cpu"))
            print(f"Optimizer loaded from {optimizer_path}")

            scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
                optimizer, mode="min", factor=0.3, patience=2
            )
            scheduler.load_state_dict(torch.load(scheduler_path, map_location="cpu"))
            print(f"Scheduler loaded from {scheduler_path}")
        else:
            scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
                optimizer, mode="min", factor=0.3, patience=2
            )

        return optimizer, scheduler

    def _get_loss_fn(self):
        return nn.BCEWithLogitsLoss()