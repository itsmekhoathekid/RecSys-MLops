from models import * 
import argparse
import yaml

def main():
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
        train_loss, train_metrics = trainer.train(train_loader)
        val_loss, val_metrics = trainer.evaluate(val_loader)

        print(f"Epoch {epoch+1}/{config['training_args']['num_epochs']}")
        print(f"Train Loss: {train_loss:.4f}, Train Metrics: {train_metrics}")
        print(f"Val Loss: {val_loss:.4f}, Val Metrics: {val_metrics}")

if __name__ == "__main__":
    main()