from torch.utils.data import Dataset
import json
import torch
import yaml

class recommenderDataset(Dataset):
    def __init__(self, config, split = "train", percent=1.0):
        self.data = self.load_json(self.get_data_path(config, split), percent)
        self.max_len = config["max_history_len"]
        self.padding_idx = config["padding_idx"]

    def get_data_path(self, config, split):
        if split == "train":
            return config["train_data_path"]
        elif split == "val":
            return config["val_data_path"]
        elif split == "test":
            return config["test_data_path"]
        else:
            raise ValueError(f"Invalid split: {split}")

    def load_json(self, jsonl_path, percent):
        with open(jsonl_path, "r") as f:
            data = [json.loads(line) for line in f]
        return data[:int(len(data) * percent)]
    
    def __len__(self):
        return len(self.data)

    def __getitem__(self, idx):
        item = {
            "user_id": self.data[idx]["user_id"],
            "hist_item_id": self.data[idx]["hist_item_id"],
            "hist_event_type": self.data[idx]["hist_event_type"],
            "hist_category": self.data[idx]["hist_category"],
            "hist_brand": self.data[idx]["hist_brand"],
            "hist_price_bucket": self.data[idx]["hist_price_bucket"],
            "hist_time": self.data[idx]["hist_time"],
            "target_item_id": self.data[idx]["target_item_id"],
            "target_category": self.data[idx]["target_category"],
            "target_brand": self.data[idx]["target_brand"],
            "target_price_bucket": self.data[idx]["target_price_bucket"],
            "event_time": self.data[idx]["event_time"],
            "label": self.data[idx]["label"]
        }

        return item
    
    def _pad_and_trim(self, seq, max_len, pad_idx):
        seq = seq[-max_len:]  # giữ phần gần nhất
        return torch.tensor([pad_idx] * (max_len - len(seq)) + seq, dtype=torch.long)
    
    
    def collate_fn(self, batch):
        # patch hist features to max_len and convert to tensor
        batch_dict = {
            "user_id" : torch.stack([torch.tensor(item["user_id"]) for item in batch]),
            "hist_item_id" : torch.stack([self._pad_and_trim(item["hist_item_id"], self.max_len, self.padding_idx) for item in batch]),
            "hist_event_type" : torch.stack([self._pad_and_trim(item["hist_event_type"], self.max_len, self.padding_idx) for item in batch]),
            "hist_category" : torch.stack([self._pad_and_trim(item["hist_category"], self.max_len, self.padding_idx) for item in batch]),
            "hist_brand" : torch.stack([self._pad_and_trim(item["hist_brand"], self.max_len, self.padding_idx) for item in batch]),
            "hist_price_bucket" : torch.stack([self._pad_and_trim(item["hist_price_bucket"], self.max_len, self.padding_idx) for item in batch]),
            "hist_time" : torch.stack([self._pad_and_trim(item["hist_time"], self.max_len, self.padding_idx) for item in batch]),
            "target_item_id" : torch.stack([torch.tensor(item["target_item_id"]) for item in batch]),
            "target_category" : torch.stack([torch.tensor(item["target_category"]) for item in batch]),
            "target_brand" : torch.stack([torch.tensor(item["target_brand"]) for item in batch]),
            "target_price_bucket" : torch.stack([torch.tensor(item["target_price_bucket"]) for item in batch]),
            "event_time" : torch.stack([torch.tensor(item["event_time"]) for item in batch]),
            "label" : torch.stack([torch.tensor(item["label"]) for item in batch])
        }
        
        return batch_dict

import logging
class Logger:
    def __init__(self):
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
        )
        logger = logging.getLogger(__name__)
        self.logger = logger
    
    def log_loss(self, loss, epoch):
        self.logger.info(f"Epoch {epoch}: Loss = {loss:.4f}")

def load_config(config_path):
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)
    return config