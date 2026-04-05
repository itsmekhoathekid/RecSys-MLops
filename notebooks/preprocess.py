import json
import math
from concurrent.futures import ProcessPoolExecutor
from tqdm.auto import tqdm
from sklearn.preprocessing import LabelEncoder

def bucketize_seconds_diff(seconds: int) -> int:
    if seconds < 60 * 5:                 # < 5 phút
        return 0
    if seconds < 60 * 30:                # < 30 phút
        return 1
    if seconds < 60 * 60 * 2:            # < 2 giờ
        return 2
    if seconds < 60 * 60 * 6:            # < 6 giờ
        return 3
    if seconds < 60 * 60 * 24:           # < 1 ngày
        return 4
    if seconds < 60 * 60 * 24 * 3:       # < 3 ngày
        return 5
    if seconds < 60 * 60 * 24 * 7:       # < 7 ngày
        return 6
    if seconds < 60 * 60 * 24 * 14:      # < 14 ngày
        return 7
    return 8                             # >= 14 ngày

def from_ts_to_bucket(ts, current_ts):
    ts = ts/1000
    current_ts = current_ts/1000
    return bucketize_seconds_diff(current_ts - ts)


def normalize_row_worker(row):
    row.pop("target_event_type", None)

    row["user_id"] = str(row.get("user_id") or "UNK_USER")
    row["target_item_id"] = str(row.get("target_item_id") or "UNK_ITEM")
    row["target_category"] = str(row.get("target_category") or "UNK_CATEGORY")
    row["target_brand"] = str(row.get("target_brand") or "UNK_BRAND")

    row["hist_item_id"] = [str(x or "UNK_ITEM") for x in row.get("hist_item_id", [])]
    row["hist_category"] = [str(x or "UNK_CATEGORY") for x in row.get("hist_category", [])]
    row["hist_brand"] = [str(x or "UNK_BRAND") for x in row.get("hist_brand", [])]

    row["hist_event_type"] = [int(x) for x in row.get("hist_event_type", [])]
    row["hist_price_bucket"] = [int(x) for x in row.get("hist_price_bucket", [])]
    row["hist_time"] = [int(x) for x in row.get("hist_time", [])]

    row["target_price"] = float(row.get("target_price", 0.0) or 0.0)
    row["target_price_log"] = math.log1p(row["target_price"])
    row["target_price_bucket"] = int(row.get("target_price_bucket", 0) or 0)
    row["target_event_type_id"] = int(row.get("target_event_type_id", 0) or 0)
    row["label"] = int(row.get("label", 0) or 0)

    inferred_hist_len = len(row["hist_item_id"])
    row["hist_len"] = int(row.get("hist_len", inferred_hist_len) or 0)

    # đồng bộ mọi history field theo hist_len thật
    true_len = min(
        row["hist_len"],
        len(row["hist_item_id"]),
        len(row["hist_category"]),
        len(row["hist_brand"]),
        len(row["hist_event_type"]),
        len(row["hist_price_bucket"]),
        len(row["hist_time"]),
    )

    row["hist_item_id"] = row["hist_item_id"][:true_len]
    row["hist_category"] = row["hist_category"][:true_len]
    row["hist_brand"] = row["hist_brand"][:true_len]
    row["hist_event_type"] = row["hist_event_type"][:true_len]
    row["hist_price_bucket"] = row["hist_price_bucket"][:true_len]
    row["hist_time"] = row["hist_time"][:true_len]
    row["hist_len"] = true_len
    row["hist_time_bucket"] = [from_ts_to_bucket(ts, row['event_time']) for ts in row["hist_time"]]

    return row


def process_chunk_normalize_worker(lines):
    rows = [normalize_row_worker(json.loads(line)) for line in lines]

    items = set()
    categories = set()
    brands = set()
    users = set()

    for row in rows:
        users.add(row["user_id"])
        items.add(row["target_item_id"])
        items.update(row["hist_item_id"])
        categories.add(row["target_category"])
        categories.update(row["hist_category"])
        brands.add(row["target_brand"])
        brands.update(row["hist_brand"])


    return rows, items, categories, brands, users


def process_chunk_encode_worker(args):
    rows, item2id, cat2id, brand2id, user2id = args

    out = []
    for row in rows:
        row["user_id"] = user2id[row["user_id"]]
        row["target_item_id"] = item2id[row["target_item_id"]]
        row["hist_item_id"] = [item2id[x] for x in row["hist_item_id"]]

        row["target_category"] = cat2id[row["target_category"]]
        row["hist_category"] = [cat2id[x] for x in row["hist_category"]]

        row["target_brand"] = brand2id[row["target_brand"]]
        row["hist_brand"] = [brand2id[x] for x in row["hist_brand"]]

        out.append(row)
    return out


def read_in_chunks(path, chunk_size):
    chunk = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            chunk.append(line)
            if len(chunk) >= chunk_size:
                yield chunk
                chunk = []
        if chunk:
            yield chunk


def preprocessing_bst_ranking_parallel(
    json_input,
    json_output,
    encoder_json_output=None,
    num_workers=8,
    chunk_size=50000,
):
    print("Pass 1: normalize + build vocabulary")

    all_items, all_categories, all_brands, all_users = set(), set(), set(), set()
    normalized_chunks = []

    with ProcessPoolExecutor(max_workers=num_workers) as ex:
        futures = [
            ex.submit(process_chunk_normalize_worker, chunk)
            for chunk in read_in_chunks(json_input, chunk_size)
        ]

        for fut in tqdm(futures, desc="Pass 1 chunks"):
            rows, items, categories, brands, users = fut.result()
            normalized_chunks.append(rows)
            all_items.update(items)
            all_categories.update(categories)
            all_brands.update(brands)
            all_users.update(users)

    print("Fitting encoders")
    item_le = LabelEncoder().fit(sorted(all_items))
    cat_le = LabelEncoder().fit(sorted(all_categories))
    brand_le = LabelEncoder().fit(sorted(all_brands))
    user_le = LabelEncoder().fit(sorted(all_users))

    item2id = {cls: int(i + 1) for i, cls in enumerate(item_le.classes_)}
    cat2id = {cls: int(i + 1) for i, cls in enumerate(cat_le.classes_)}
    brand2id = {cls: int(i + 1) for i, cls in enumerate(brand_le.classes_)}
    user2id = {cls: int(i + 1) for i, cls in enumerate(user_le.classes_)}

    print("Pass 2: encode + save")
    with open(json_output, "w", encoding="utf-8") as out_f:
        with ProcessPoolExecutor(max_workers=num_workers) as ex:
            futures = [
                ex.submit(
                    process_chunk_encode_worker,
                    (rows, item2id, cat2id, brand2id, user2id)
                )
                for rows in normalized_chunks
            ]

            for fut in tqdm(futures, desc="Pass 2 chunks"):
                encoded_rows = fut.result()
                for row in encoded_rows:
                    out_f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"Saved preprocessed JSONL to: {json_output}")

    if encoder_json_output is not None:
        encoder_json = {
            "item2id": item2id,
            "category2id": cat2id,
            "brand2id": brand2id,
            "user2id": user2id,
            "event2id": {
                "NEGATIVE": 0,
                "cart": 1,
                "purchase": 2,
                "view": 3,
            },
            "meta": {
                "num_items": len(item2id),
                "num_categories": len(cat2id),
                "num_brands": len(brand2id),
                "num_users": len(user2id),
                "chunk_size": chunk_size,
                "num_workers": num_workers,
            },
        }

        with open(encoder_json_output, "w", encoding="utf-8") as f:
            json.dump(encoder_json, f, ensure_ascii=False, indent=2)

        print(f"Saved encoder mapping JSON to: {encoder_json_output}")
from pathlib import Path
from collections import defaultdict


def split_preprocessed_bst_jsonl_by_time(
    input_jsonl: str,
    output_dir: str,
    train_ratio: float = 0.8,
    val_ratio: float = 0.1,
):
    """
    Split encoded/preprocessed BST JSONL into train/val/test by temporal groups.

    Group key = (user_id, event_time)
    This keeps one positive + its negatives together.

    Output:
      - train.jsonl
      - val.jsonl
      - test.jsonl
    """
    assert 0 < train_ratio < 1
    assert 0 <= val_ratio < 1
    assert train_ratio + val_ratio < 1

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print("\n[Split] Reading all rows and grouping by (user_id, event_time)")
    groups = defaultdict(list)

    with open(input_jsonl, "r", encoding="utf-8") as f:
        for line in tqdm(f, desc="Reading encoded JSONL"):
            row = json.loads(line)
            key = (row["user_id"], row["event_time"])
            groups[key].append(row)

    print(f"Total groups: {len(groups):,}")

    # Sort groups by event_time ascending, then user_id
    sorted_groups = sorted(groups.items(), key=lambda x: (x[0][1], x[0][0]))

    n_groups = len(sorted_groups)
    train_end = int(n_groups * train_ratio)
    val_end = int(n_groups * (train_ratio + val_ratio))

    train_groups = sorted_groups[:train_end]
    val_groups = sorted_groups[train_end:val_end]
    test_groups = sorted_groups[val_end:]

    print(f"Train groups: {len(train_groups):,}")
    print(f"Val groups:   {len(val_groups):,}")
    print(f"Test groups:  {len(test_groups):,}")

    def write_groups(path, grouped_rows):
        n_rows = 0
        with open(path, "w", encoding="utf-8") as out_f:
            for _, rows in tqdm(grouped_rows, desc=f"Writing {Path(path).name}"):
                for row in rows:
                    out_f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    n_rows += 1
        return n_rows

    train_path = output_dir / "train.jsonl"
    val_path = output_dir / "val.jsonl"
    test_path = output_dir / "test.jsonl"

    train_rows = write_groups(train_path, train_groups)
    val_rows = write_groups(val_path, val_groups)
    test_rows = write_groups(test_path, test_groups)

    print("\n[Split] Done")
    print(f"Train rows: {train_rows:,} -> {train_path}")
    print(f"Val rows:   {val_rows:,} -> {val_path}")
    print(f"Test rows:  {test_rows:,} -> {test_path}")

    split_meta = {
        "input_jsonl": input_jsonl,
        "output_dir": str(output_dir),
        "train_ratio": train_ratio,
        "val_ratio": val_ratio,
        "test_ratio": 1.0 - train_ratio - val_ratio,
        "num_groups": n_groups,
        "train_groups": len(train_groups),
        "val_groups": len(val_groups),
        "test_groups": len(test_groups),
        "train_rows": train_rows,
        "val_rows": val_rows,
        "test_rows": test_rows,
    }

    with open(output_dir / "split_meta.json", "w", encoding="utf-8") as f:
        json.dump(split_meta, f, ensure_ascii=False, indent=2)

    print(f"Saved split metadata -> {output_dir / 'split_meta.json'}")
    return split_meta

if __name__ == "__main__":
    preprocessing_bst_ranking_parallel(
        json_input="./notebooks/data/2019-Oct-bst-ranking-1m.jsonl",
        json_output="./notebooks/data/2019-Oct-bst-ranking-1m-preprocessed.jsonl",
        encoder_json_output="./notebooks/data/2019-Oct-bst-ranking-1m-encoders.json",
        num_workers=16,
        chunk_size=50000,
    )

    split_preprocessed_bst_jsonl_by_time(
        input_jsonl="./notebooks/data/2019-Oct-bst-ranking-1m-preprocessed.jsonl",
        output_dir="./notebooks/data/bst_split",
        train_ratio=0.8,
        val_ratio=0.1,
    )