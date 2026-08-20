# 🚌 SPTrans Rain Impact Pipeline 🌧️

An end-to-end data engineering pipeline designed to measure and analyze how rainfall affects public bus operational speeds in São Paulo.



### 🎯 Core Thesis

> **"Rainfall reduces the operational speed of public buses in São Paulo, with speed loss being directly proportional to precipitation intensity."**


## 📌 Objectives

1. **Automate Real-Time Ingestion:** Extract real-time bus positions from the SPTrans Olho Vivo API and weather station readings from the CEMADEN API.
2. **Implement Medallion Architecture:** Structure data through Bronze (raw JSONs), Silver (cleaned & spatio-temporally joined), and Gold (aggregated metrics) layers.
3. **Spatio-Temporal Alignment:** Match bus vehicle locations with the nearest rainfall station using asymmetrical *as-of* temporal joins.
4. **Analytical Delivery:** Build query-ready datasets and visualizations to quantify speed reduction per line under different rain intensity levels.


## 🔌 Data Sources

* **[SPTrans Olho Vivo API](https://www.sptrans.com.br/desenvolvedores/):** Real-time GPS coordinates of active buses and static GTFS feeds.
* **[CEMADEN PED](https://www.gov.br/cemaden/):** Rain gauge station readings across the São Paulo metropolitan area.


## 📄 License

This project is developed for academic purposes under the Data Engineering course at UNIFEI.