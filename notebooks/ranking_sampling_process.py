import pandas as pd
import numpy as np
from tqdm.auto import tqdm
from concurrent.futures import ProcessPoolExecutor, as_completed
import math


def _log(step, msg):
    print(f"\n[Step {step}] {msg}")


class PopularitySampler:
    def __init__(self, item_counts: dict, alpha: float = 1.0, seed: int = 42):
        self.rng = np.random.default_rng(seed)
        items = sorted(item_counts.keys())
        counts = np.array([item_counts[i] for i in items], dtype=np.float64)
        probs = np.power(counts, alpha)
        probs = probs / probs.sum()
        self.items = np.array(items)
        self.probs = probs

    def sample_unseen(self, seen_items: set, positive_item, k: int):
        negatives = []
        forbid = set(seen_items)
        forbid.add(positive_item)

        allowed_items = [x for x in self.items if x not in forbid]
        if len(allowed_items) == 0:
            return negatives

        while len(negatives) < k:
            sampled = self.rng.choice(
                self.items,
                size=(k - len(negatives)) * 3,
                replace=True,
                p=self.probs,
            )
            for item in sampled:
                if item not in forbid:
                    negatives.append(item)
                if len(negatives) == k:
                    break
        return negatives


def process_user_chunk(
    chunk_df: pd.DataFrame,
    item_meta: dict,
    item_counts: dict,
    min_history_len: int,
    max_history_len: int,
    num_negatives: int,
    neg_alpha: float,
    positive_events: tuple,
    history_events: tuple,
    random_state: int,
):
    sampler = PopularitySampler(
        item_counts=item_counts,
        alpha=neg_alpha,
        seed=random_state,
    )

    rows = []

    grouped = chunk_df.groupby("user_id", sort=False)
    for user_id, g in grouped:
        g = g.sort_values("event_time").reset_index(drop=True)

        hist_items = []
        hist_event_types = []
        hist_categories = []
        hist_brands = []
        hist_price_buckets = []
        hist_times = []
        seen_items = set()

        for i in range(len(g)):
            row = g.iloc[i]
            cur_item = row["product_id"]
            cur_event = row["event_type"]

            if cur_event in positive_events and len(hist_items) >= min_history_len:
                cur_hist_items = hist_items[-max_history_len:]
                cur_hist_event_types = hist_event_types[-max_history_len:]
                cur_hist_categories = hist_categories[-max_history_len:]
                cur_hist_brands = hist_brands[-max_history_len:]
                cur_hist_price_buckets = hist_price_buckets[-max_history_len:]
                cur_hist_times = hist_times[-max_history_len:]

                rows.append({
                    "user_id": user_id,
                    "event_time": row["event_time"],
                    "label": 1,
                    "target_item_id": cur_item,
                    "target_category": row["category_code"],
                    "target_brand": row["brand"],
                    "target_price": row["price"],
                    "target_price_bucket": row["price_bucket"],
                    "target_event_type": cur_event,
                    "target_event_type_id": row["event_type_id"],
                    "hist_item_id": cur_hist_items.copy(),
                    "hist_event_type": cur_hist_event_types.copy(),
                    "hist_category": cur_hist_categories.copy(),
                    "hist_brand": cur_hist_brands.copy(),
                    "hist_price_bucket": cur_hist_price_buckets.copy(),
                    "hist_time": cur_hist_times.copy(),
                    "hist_len": len(cur_hist_items),
                })

                neg_items = sampler.sample_unseen(
                    seen_items=seen_items,
                    positive_item=cur_item,
                    k=num_negatives,
                )

                for neg_item in neg_items:
                    neg_meta = item_meta[neg_item]
                    rows.append({
                        "user_id": user_id,
                        "event_time": row["event_time"],
                        "label": 0,
                        "target_item_id": neg_item,
                        "target_category": neg_meta["category_code"],
                        "target_brand": neg_meta["brand"],
                        "target_price": neg_meta["price"],
                        "target_price_bucket": neg_meta["price_bucket"],
                        "target_event_type": "NEGATIVE",
                        "target_event_type_id": 0,
                        "hist_item_id": cur_hist_items.copy(),
                        "hist_event_type": cur_hist_event_types.copy(),
                        "hist_category": cur_hist_categories.copy(),
                        "hist_brand": cur_hist_brands.copy(),
                        "hist_price_bucket": cur_hist_price_buckets.copy(),
                        "hist_time": cur_hist_times.copy(),
                        "hist_len": len(cur_hist_items),
                    })

            if cur_event in history_events:
                hist_items.append(cur_item)
                hist_event_types.append(row["event_type_id"])
                hist_categories.append(row["category_code"])
                hist_brands.append(row["brand"])
                hist_price_buckets.append(row["price_bucket"])
                hist_times.append(row["event_time"])
                seen_items.add(cur_item)

    return rows


def split_user_chunks(user_ids, n_chunks):
    chunk_size = math.ceil(len(user_ids) / n_chunks)
    return [user_ids[i:i + chunk_size] for i in range(0, len(user_ids), chunk_size)]


def build_bst_ranking_dataset_mp(
    input_csv: str,
    output_jsonl: str,
    min_history_len: int = 2,
    max_history_len: int = 20,
    num_negatives: int = 3,
    neg_alpha: float = 0.75,
    positive_events=("cart", "purchase"),
    history_events=("view", "cart", "purchase"),
    random_state: int = 42,
    num_workers: int = 4,
):
    _log(1, "Reading sampled recsys CSV")
    df = pd.read_csv(input_csv)
    print(f"Loaded rows={len(df):,}")

    _log(2, "Basic preprocessing")
    df["event_time"] = pd.to_datetime(df["event_time"], utc=True, errors="coerce")
    df = df.dropna(subset=["event_time", "user_id", "product_id", "event_type"])
    df = df.sort_values(["user_id", "event_time"]).reset_index(drop=True)

    df["category_code"] = df["category_code"].fillna("UNK_CATEGORY")
    df["brand"] = df["brand"].fillna("UNK_BRAND")
    df["price"] = df["price"].fillna(0.0)

    df["price_bucket"] = pd.qcut(
        df["price"].rank(method="first"),
        q=min(10, df["price"].nunique()),
        labels=False,
        duplicates="drop",
    )
    df["price_bucket"] = df["price_bucket"].fillna(0).astype(int)

    event2id = {e: i + 1 for i, e in enumerate(sorted(df["event_type"].unique()))}
    df["event_type_id"] = df["event_type"].map(event2id)

    print(f"Users={df['user_id'].nunique():,}, Items={df['product_id'].nunique():,}")
    print(f"Event types={event2id}")

    _log(3, "Building item metadata and popularity counts")
    item_counts = df["product_id"].value_counts().to_dict()

    item_meta = (
        df.sort_values("event_time")
        .groupby("product_id", as_index=False)
        .first()[["product_id", "category_code", "brand", "price", "price_bucket"]]
        .set_index("product_id")
        .to_dict("index")
    )

    user_ids = df["user_id"].drop_duplicates().tolist()
    chunks = split_user_chunks(user_ids, num_workers)
    print(f"Total users={len(user_ids):,}, chunks={len(chunks):,}, workers={num_workers}")

    _log(4, "Multiprocessing user chunks")
    all_rows = []

    with ProcessPoolExecutor(max_workers=num_workers) as ex:
        futures = []
        for idx, chunk_user_ids in enumerate(chunks):
            chunk_df = df[df["user_id"].isin(chunk_user_ids)].copy()
            futures.append(
                ex.submit(
                    process_user_chunk,
                    chunk_df,
                    item_meta,
                    item_counts,
                    min_history_len,
                    max_history_len,
                    num_negatives,
                    neg_alpha,
                    positive_events,
                    history_events,
                    random_state + idx,
                )
            )

        for fut in tqdm(as_completed(futures), total=len(futures), desc="Finished chunks"):
            all_rows.extend(fut.result())

    _log(5, "Building final dataframe")
    bst_df = pd.DataFrame(all_rows)
    print(f"Built BST rows={len(bst_df):,}")

    _log(6, "Saving")
    bst_df = bst_df.sort_values(["user_id", "event_time", "label"], ascending=[True, True, False]).reset_index(drop=True)
    bst_df.to_json(output_jsonl, orient="records", lines=True)
    print(f"Saved to {output_jsonl}")

    stats = {
        "rows": len(bst_df),
        "users": bst_df["user_id"].nunique(),
        "positive_rows": int((bst_df["label"] == 1).sum()),
        "negative_rows": int((bst_df["label"] == 0).sum()),
        "avg_hist_len": float(bst_df["hist_len"].mean()) if len(bst_df) else 0.0,
        "target_items": bst_df["target_item_id"].nunique(),
    }

    _log(7, "Final stats")
    for k, v in stats.items():
        print(f"- {k}: {v}")

    return bst_df, stats

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Build BST ranking dataset with multiprocessing")
    parser.add_argument("--input_csv", type=str, required=True, help="Path to sampled recsys CSV")
    parser.add_argument("--output_jsonl", type=str, required=True, help="Path to output JSONL")
    parser.add_argument("--min_history_len", type=int, default=2)
    parser.add_argument("--max_history_len", type=int, default=20)
    parser.add_argument("--num_negatives", type=int, default=3)
    parser.add_argument("--neg_alpha", type=float, default=0.75)
    parser.add_argument("--num_workers", type=int, default=4)
    parser.add_argument("--random_state", type=int, default=42)

    args = parser.parse_args()

    bst_df, stats = build_bst_ranking_dataset_mp(
        input_csv=args.input_csv,
        output_jsonl=args.output_jsonl,
        min_history_len=args.min_history_len,
        max_history_len=args.max_history_len,
        num_negatives=args.num_negatives,
        neg_alpha=args.neg_alpha,
        positive_events=("cart", "purchase"),
        history_events=("view", "cart", "purchase"),
        random_state=args.random_state,
        num_workers=args.num_workers,
    )

    print("\nDone.")
    print(stats)