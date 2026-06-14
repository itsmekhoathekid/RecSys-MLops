from models import * 
import argparse
import yaml

def train():
    args = argparse.ArgumentParser()
    args.add_argument("--config_path", type=str, default="./config/bst.yaml")
    config = args.parse_args()
    config = load_config(config.config_path)

    trainer = Trainer(config)
    training_data = recommenderDataset(config["data_args"], split="train", percent = 0.1)
    val_data = recommenderDataset(config["data_args"], split="val", percent = 0.1)

    train_loader = trainer.get_data_loader(training_data, shuffle=config["data_args"]["shuffle"])
    val_loader = trainer.get_data_loader(val_data, shuffle=False)

    for epoch in range(config["training_args"]["num_epochs"]):
        train_metrics = trainer.train(train_loader)
        val_metrics = trainer.evaluate(val_loader)

        for metric_name, metric_value in train_metrics.items():
            trainer.logger.log(f"train/{metric_name}", metric_value, epoch)

        for metric_name, metric_value in val_metrics.items():
            trainer.logger.log(f"val/{metric_name}", metric_value, epoch)

        ndcg_10_val = val_metrics.get("ndcg@10", 0)
        best_score = trainer.get_best_score()

        if ndcg_10_val > best_score:
            trainer.save_model(epoch, ndcg_10_val)
            trainer.best_score = ndcg_10_val
            print(f"New best model saved with NDCG@10: {ndcg_10_val:.4f}")
        else:
            print(f"No improvement in NDCG@10: {ndcg_10_val:.4f} (best: {best_score:.4f})")

        
if __name__ == "__main__":
    train()