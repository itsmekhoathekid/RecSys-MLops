# GCP Cost Estimate

## Estimate setting hien tai khi up full

- **Node compute:** khoang **$0.45-0.55/gio**
  - 1 x `e2-standard-8` cho data platform
  - 1 x `e2-standard-4` cho ML system
  - GPU pool dang `0`, nen chua tinh GPU.

- **GKE cluster management:** **$0.10/gio**, nhung co the duoc free-tier credit offset neu billing account con eligible cho 1 zonal cluster. Reference: Google Cloud GKE pricing.

- **Persistent disks:** khoang **$0.03/gio** luc up, tinh boot disks + PVC khoang **~219 GiB pd-balanced**. Google list Balanced provisioned space o muc **$0.000136986/GiB-hour**. Reference: Google Cloud disk pricing.

- **Gateway/load balancer:** khoang **$0.025-0.04/gio** neu ingress LoadBalancer con forwarding rule. Google co charge forwarding rule, vi du first 5 global forwarding rules **$0.025/hour**; regional co the khac chut theo region. Reference: Google Cloud VPC network pricing.

- **Logging/Monitoring/Artifact Registry/Cloud Build:** tuy traffic/build, tam cong **$0.02-0.10/gio** neu log nhieu hoac vua build image.

**Tong thuc te luc up:** khoang **$0.65-0.80/gio**, tuc **$15-19/ngay** neu de 24/7. Neu free tier GKE offset duoc cluster fee thi con khoang **$0.55-0.70/gio**.

## Estimate luc down sau `make gcp-services-down`

Luc down hien tai sau `make gcp-services-down`: khong con node, nen compute gan nhu `0`. Con lai chu yeu:

- **GKE control plane:** **$0.10/gio** neu khong duoc free-tier offset.
- **PVC disks ~99 GiB:** khoang **$0.014/gio**, co **~$0.34/ngay**.
- **LB/forwarding rule:** neu gateway resource van ton tai, them khoang **$0.025-0.04/gio**.

## Demo recommendation

Nen de demo kieu nay hop ly nhat la: **up 1-2 tieng de capture proof, xong down ngay**.

- Mot buoi demo 2 tieng tam **$1.3-1.6**.
- De qua dem 10-12 tieng la bay **$7-10** rat de.
