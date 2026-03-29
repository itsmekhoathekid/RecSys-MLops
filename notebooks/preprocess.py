import json
import math
from concurrent.futures import ProcessPoolExecutor
from tqdm.auto import tqdm
from sklearn.preprocessing import LabelEncoder


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

if __name__ == "__main__":
    preprocessing_bst_ranking_parallel(
        json_input="./notebooks/data/2019-Oct-bst-ranking-1m.jsonl",
        json_output="./notebooks/data/2019-Oct-bst-ranking-1m-preprocessed.json",
        encoder_json_output="./notebooks/data/2019-Oct-bst-ranking-1m-encoders.json",
        num_workers=16,
        chunk_size=50000,
    )