# AIDE1-project

dataset picked : https://www.kaggle.com/datasets/computingvictor/transactions-fraud-datasets
problems picked : churn prediction, recommendation system
    Predict whether a customer will stop using the service


locked recommendation system
dataset : https://www.kaggle.com/datasets/rsrishav/youtube-trending-video-dataset/data 
highly relevant proj : 
    techstack : https://github.com/thanhphat-19/card-approval-prediction 
    idea : https://github.com/nguyenthai-duong/Ecommerce-Recommender-System-On-Aws-With-Mlops 
    
    model : https://github.com/jiwidi/Behavior-Sequence-Transformer-Pytorch

    dataset champion : https://www.kaggle.com/datasets/mkechinov/ecommerce-behavior-data-from-multi-category-store/ 
        -> extract into smaller dataset 
    dataset : https://www.kaggle.com/datasets/shikharg97/movielens-1m

    



references : 
    https://github.com/phuchoang2603/realtime-credit-card-fraud-detection
    https://github.com/thanhphat-19/card-approval-prediction
    https://github.com/bmd1905/Customer-Purchase-Prediction-ML-System 
    https://www.facebook.com/share/p/1AdzQy7wnR/ 
    https://www.linkedin.com/in/quan-dang/recent-activity/all/
    

python ./notebooks/ranking_sampling_process.py \ 
  --input_csv notebooks/data/2019-Oct-recsys-1m.csv \
  --output_jsonl notebooks/data/2019-Oct-bst-ranking-1m.jsonl \
  --min_history_len 2 \
  --max_history_len 20 \
  --num_negatives 3 \
  --neg_alpha 0.75 \
  --num_workers 4 \
  --random_state 42


python train.py --config_path ./config/bst.yaml