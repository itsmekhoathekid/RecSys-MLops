from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import torch
from torch.utils.data import Dataset


@dataclass
class BSTSampleConfig:
    user_col: str = "user_id"
    item_col: str = "item_id"
    time_col: str = "timestamp"
    category_col: Optional[str] = "category_id"

    max_history_len: int = 50
    min_history_len: int = 1

    num_negatives: int = 1
    neg_sampling_alpha: float = 0.75  # p(i) ~ freq(i)^alpha
    seed: int = 42


class BSTDatasetBuilderProbNeg:
    """
    Build BST-style ranking dataset:
      input  = user + history sequence + target item (+ optional side features)
      target = label 1/0

    Positive rows:
      observed next interactions

    Negative rows:
      sampled from unobserved items with probability
      proportional to item popularity^alpha
    """

    def __init__(self, config: BSTSampleConfig):
        self.config = config
        self.rng = np.random.default_rng(config.seed)

    def _build_item_mappings(
        self, df: pd.DataFrame
    ) -> Tuple[Dict, Dict, Dict, Dict]:
        cfg = self.config

        user_values = sorted(df[cfg.user_col].unique().tolist())
        item_values = sorted(df[cfg.item_col].unique().tolist())

        user2id = {u: i + 1 for i, u in enumerate(user_values)}
        item2id = {it: i + 1 for i, it in enumerate(item_values)}

        id2user = {v: k for k, v in user2id.items()}
        id2item = {v: k for k, v in item2id.items()}

        return user2id, item2id, id2user, id2item

    def _build_category_mapping(
        self, df: pd.DataFrame
    ) -> Tuple[Optional[Dict], Optional[Dict]]:
        cfg = self.config
        if cfg.category_col is None:
            return None, None

        cat_values = sorted(df[cfg.category_col].dropna().unique().tolist())
        cat2id = {c: i + 1 for i, c in enumerate(cat_values)}

        item_to_cat_raw = (
            df[[cfg.item_col, cfg.category_col]]
            .drop_duplicates(subset=[cfg.item_col])
            .set_index(cfg.item_col)[cfg.category_col]
            .to_dict()
        )

        return cat2id, item_to_cat_raw

    def _compute_item_sampling_probs(
        self,
        df: pd.DataFrame,
        item2id: Dict
    ) -> np.ndarray:
        cfg = self.config

        freq = df[cfg.item_col].value_counts().to_dict()

        num_items = len(item2id)
        probs = np.zeros(num_items + 1, dtype=np.float64)  # index 0 unused

        for raw_item, idx in item2id.items():
            count = freq.get(raw_item, 0)
            probs[idx] = float(count) ** cfg.neg_sampling_alpha

        total = probs.sum()
        if total == 0:
            probs[1:] = 1.0 / num_items
        else:
            probs /= total

        return probs

    def _sample_negatives_for_user(
        self,
        user_seen_items: set,
        item_ids_all: np.ndarray,
        global_probs: np.ndarray,
        k: int
    ) -> List[int]:
        """
        Sample negatives from items not seen by this user.
        Uses renormalized probabilities over allowed items only.
        """
        allowed_mask = np.array([item_id not in user_seen_items for item_id in item_ids_all])
        allowed_items = item_ids_all[allowed_mask]

        if len(allowed_items) == 0:
            return []

        allowed_probs = global_probs[allowed_items].copy()
        prob_sum = allowed_probs.sum()

        if prob_sum <= 0:
            allowed_probs = np.ones_like(allowed_probs, dtype=np.float64) / len(allowed_probs)
        else:
            allowed_probs = allowed_probs / prob_sum

        replace = len(allowed_items) < k
        sampled = self.rng.choice(allowed_items, size=k, replace=replace, p=allowed_probs)
        return sampled.tolist()

    def build(self, df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict]:
        cfg = self.config

        required_cols = [cfg.user_col, cfg.item_col, cfg.time_col]
        for col in required_cols:
            if col not in df.columns:
                raise ValueError(f"Missing required column: {col}")

        if cfg.category_col is not None and cfg.category_col not in df.columns:
            raise ValueError(f"Missing category column: {cfg.category_col}")

        data = df.copy()
        data = data.sort_values([cfg.user_col, cfg.time_col]).reset_index(drop=True)

        user2id, item2id, id2user, id2item = self._build_item_mappings(data)
        cat2id, item_to_cat_raw = self._build_category_mapping(data)

        data["user_id_enc"] = data[cfg.user_col].map(user2id)
        data["item_id_enc"] = data[cfg.item_col].map(item2id)

        if cfg.category_col is not None:
            data["category_id_enc"] = data[cfg.category_col].map(cat2id)

        # item -> category_id_enc
        item_to_cat_enc = {}
        if cfg.category_col is not None:
            for raw_item, raw_cat in item_to_cat_raw.items():
                item_to_cat_enc[item2id[raw_item]] = cat2id.get(raw_cat, 0)

        global_probs = self._compute_item_sampling_probs(data, item2id)
        item_ids_all = np.arange(1, len(item2id) + 1)

        rows: List[Dict] = []

        for user_raw, group in data.groupby(cfg.user_col, sort=False):
            group = group.sort_values(cfg.time_col).reset_index(drop=True)

            user_id = user2id[user_raw]
            items = group["item_id_enc"].tolist()
            cats = group["category_id_enc"].tolist() if cfg.category_col is not None else None

            user_seen_items = set(items)

            for i in range(1, len(items)):
                hist_items = items[:i]
                if len(hist_items) < cfg.min_history_len:
                    continue

                hist_items = hist_items[-cfg.max_history_len:]
                hist_len = len(hist_items)

                pos_target = items[i]

                pos_row = {
                    "user_id": user_id,
                    "hist_item_id": hist_items,
                    "hist_len": hist_len,
                    "target_item_id": pos_target,
                    "label": 1.0,
                }

                if cfg.category_col is not None:
                    hist_cats = cats[:i][-cfg.max_history_len:]
                    pos_row["hist_category_id"] = hist_cats
                    pos_row["target_category_id"] = cats[i]

                rows.append(pos_row)

                neg_items = self._sample_negatives_for_user(
                    user_seen_items=user_seen_items,
                    item_ids_all=item_ids_all,
                    global_probs=global_probs,
                    k=cfg.num_negatives,
                )

                for neg_item in neg_items:
                    neg_row = {
                        "user_id": user_id,
                        "hist_item_id": hist_items,
                        "hist_len": hist_len,
                        "target_item_id": int(neg_item),
                        "label": 0.0,
                    }

                    if cfg.category_col is not None:
                        hist_cats = cats[:i][-cfg.max_history_len:]
                        neg_row["hist_category_id"] = hist_cats
                        neg_row["target_category_id"] = item_to_cat_enc.get(int(neg_item), 0)

                    rows.append(neg_row)

        result = pd.DataFrame(rows)

        meta = {
            "user2id": user2id,
            "item2id": item2id,
            "id2user": id2user,
            "id2item": id2item,
            "cat2id": cat2id,
            "item_to_cat_enc": item_to_cat_enc,
            "num_users": len(user2id),
            "num_items": len(item2id),
            "num_categories": len(cat2id) if cat2id is not None else 0,
            "global_item_probs": global_probs,
        }
        return result, meta


class BSTTorchDataset(Dataset):
    def __init__(self, df: pd.DataFrame, use_category: bool = True):
        self.df = df.reset_index(drop=True)
        self.use_category = use_category and ("hist_category_id" in df.columns)

    def __len__(self):
        return len(self.df)

    def __getitem__(self, idx: int):
        row = self.df.iloc[idx]

        sample = {
            "user_id": int(row["user_id"]),
            "hist_item_id": row["hist_item_id"],
            "hist_len": int(row["hist_len"]),
            "target_item_id": int(row["target_item_id"]),
            "label": float(row["label"]),
        }

        if self.use_category:
            sample["hist_category_id"] = row["hist_category_id"]
            sample["target_category_id"] = int(row["target_category_id"])

        return sample


def bst_collate_fn(batch: List[Dict]) -> Dict[str, torch.Tensor]:
    max_len = max(x["hist_len"] for x in batch)

    user_ids = []
    hist_items = []
    hist_lens = []
    target_items = []
    labels = []

    has_category = "hist_category_id" in batch[0]
    hist_cats = []
    target_cats = []

    for x in batch:
        pad_len = max_len - x["hist_len"]

        padded_hist_items = x["hist_item_id"] + [0] * pad_len

        user_ids.append(x["user_id"])
        hist_items.append(padded_hist_items)
        hist_lens.append(x["hist_len"])
        target_items.append(x["target_item_id"])
        labels.append(x["label"])

        if has_category:
            padded_hist_cats = x["hist_category_id"] + [0] * pad_len
            hist_cats.append(padded_hist_cats)
            target_cats.append(x["target_category_id"])

    output = {
        "user_id": torch.tensor(user_ids, dtype=torch.long),
        "hist_item_id": torch.tensor(hist_items, dtype=torch.long),
        "hist_len": torch.tensor(hist_lens, dtype=torch.long),
        "target_item_id": torch.tensor(target_items, dtype=torch.long),
        "label": torch.tensor(labels, dtype=torch.float32),
    }

    if has_category:
        output["hist_category_id"] = torch.tensor(hist_cats, dtype=torch.long)
        output["target_category_id"] = torch.tensor(target_cats, dtype=torch.long)

    return output



raw_df = pd.DataFrame(
    {
        "user_id": [
            "A", "A", "A", "A",
            "B", "B", "B",
            "C", "C", "C", "C"
        ],
        "item_id": [
            "B", "C", "D", "E",
            "A1", "A2", "A3",
            "B", "A2", "X", "Y"
        ],
        "timestamp": [1, 2, 3, 4, 1, 2, 3, 1, 2, 3, 4],
        "category_id": [
            "c1", "c2", "c2", "c3",
            "c1", "c1", "c2",
            "c1", "c1", "c3", "c3"
        ],
    }
)

config = BSTSampleConfig(
    user_col="user_id",
    item_col="item_id",
    time_col="timestamp",
    category_col="category_id",
    max_history_len=5,
    min_history_len=1,
    num_negatives=2,
    neg_sampling_alpha=0.75,
    seed=2026,
)

builder = BSTDatasetBuilderProbNeg(config)
train_df, meta = builder.build(raw_df)

print(train_df.head(12))
print("num_items =", meta["num_items"])
print("global probs =", meta["global_item_probs"][1:10])


