from airflow import DAG
from airflow.operators.trigger_dagrun import TriggerDagRunOperator
from datetime import datetime, timedelta

default_args = {
    'owner': 'airflow',
    'depends_on_past': False,
    'retries': 0,
    'retry_delay': timedelta(minutes=5),
}

with DAG(
    dag_id='trigger_dataset_ETL_series',
    default_args=default_args,
    description='Triggers GLODAP -> GEBCO -> OSM Water Hexes DAGs in series',
    schedule_interval=None,  # Manual trigger only
    start_date=datetime(2025, 1, 1),
    catchup=False,
) as dag:

    trigger_glodap = TriggerDagRunOperator(
        task_id='trigger_glodap_dag',
        trigger_dag_id='dataset_ETL_GLODAP_obs',  # your actual DAG ID
        wait_for_completion=True,
        reset_dag_run=True,
    )

    trigger_gebco = TriggerDagRunOperator(
        task_id='trigger_gebco_dag',
        trigger_dag_id='dataset_ETL_GEBCO_netcdf_TID_to_pgsql',
        wait_for_completion=True,
        reset_dag_run=True,
    )

    trigger_gbif = TriggerDagRunOperator(
        task_id='trigger_gbif_dag',
        trigger_dag_id='dataset_ETL_GBIF_occurrence',
        wait_for_completion=True,
        reset_dag_run=True,
    )

    # Set execution order: GLODAP -> GEBCO -> OSM
    trigger_glodap >> trigger_gebco >> trigger_gbif