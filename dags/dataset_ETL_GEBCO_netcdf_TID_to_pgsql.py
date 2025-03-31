from airflow import DAG
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.operators.python import PythonOperator
from datetime import datetime, timedelta
import requests
import zipfile
import logging
import subprocess
import os

def download_and_unzip(url):
    """Download the ZIP file, extract contents, and return the extracted directory path."""
    zip_path = "/mnt/data/gebco_2024.zip"

    response = requests.get(url, stream=True)
    response.raise_for_status()

    with open(zip_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)

    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        zip_ref.extractall("/mnt/data/")

    logging.info("Extracted files to: /mnt/data/")


def netcdf_to_pgsql(table_name, db_name, db_user, srid):
    """Loads a raster file into a PostgreSQL/PostGIS database using raster2pgsql."""
    file_path = "/mnt/data/GEBCO_2024_TID.nc"
    sql_file_path = "/mnt/data/gebco_2024.sql"
    
    logging.info('Creating SQL file...')
    # Create the SQL file using raster2pgsql
    command = f'raster2pgsql -s {srid} -t 256x256 -I -C -c "{file_path}" "{table_name}" > {sql_file_path}'
    
    # Execute raster2pgsql command to generate the SQL file
    subprocess.run(command, shell=True, check=True)

    logging.info('SQL file created, loading...')
    # Now execute the generated SQL file in the PostgreSQL database using psql
    pg_hook = PostgresHook(postgres_conn_id="oceexp-db")

    try:
        # Accessing connection details from the hook
        conn = pg_hook.get_conn()
        host = conn.host
        port = conn.port
        user = conn.user
        password = conn.password

        logging.info(f"Loading SQL file {sql_file_path} into database...")
        
        # Run the SQL file using the PostgreSQL connection info from Airflow
        pg_hook.run(f"psql -d {db_name} -U {user} -f {sql_file_path} -h {host} -p {port}", autocommit=True)
        
        logging.info("SQL file loaded successfully into PostgreSQL!")
    except Exception as e:
        logging.error(f"SQL execution failed: {e}")
        raise


def assign_gebcoTID_hex():
    """Assign hexes to GEBCO data."""
    sql_statements = """
        ALTER TABLE gebco_2024 ADD COLUMN location GEOMETRY(point, 4326);
        UPDATE gebco_2024 SET location = ST_SETSRID(ST_MakePoint(cast(longitude as float), cast(latitude as float)),4326);

        ALTER TABLE gebco_2024 ADD COLUMN hex_05 H3INDEX;
        UPDATE gebco_2024 SET hex_05 = H3_LAT_LNG_TO_CELL(location, 5);
    """

    # Initialize PostgresHook
    pg_hook = PostgresHook(postgres_conn_id="oceexp-db")

    try:
        logging.info("Executing SQL statements...")
        pg_hook.run(sql_statements, autocommit=True)
        logging.info("SQL execution completed successfully!")
    except Exception as e:
        logging.error(f"SQL execution failed: {e}")
        raise


default_args = {
    'owner': 'airflow',
    'depends_on_past': False,
    'email_on_failure': False,
    'email_on_retry': False,
    'retries': 0,
    'retry_delay': timedelta(minutes=5),
}

netcdf_url = "https://www.bodc.ac.uk/data/open_download/gebco/gebco_2024_tid/zip/"
table_name = "gebco_2024"
postgres_conn_id = "oceexp-db"
postgres_conn_user = "oceexp-db"

dag = DAG(
    'dataset_ETL_GEBCO_netcdf_TID_to_pgsql',
    default_args=default_args,
    description='Download, unzip, process, and load GEBCO NetCDF raster into PostGIS',
    schedule_interval=None,  
    start_date=datetime(2025, 1, 1),
    catchup=False,
)

download_and_unzip_task = PythonOperator(
    task_id='download_and_unzip',
    python_callable=download_and_unzip,
    op_args=[netcdf_url],
    dag=dag
)

netcdf_to_pgsql_task = PythonOperator(
    task_id='netcdf_to_pgsql',
    python_callable=netcdf_to_pgsql,
    op_kwargs={
        'table_name': table_name,
        'db_name': postgres_conn_id,
        'db_user': postgres_conn_user,
        'srid': "4326"
    },
    dag=dag
)

assign_hexes_to_gebco_task = PythonOperator(
    task_id='assign_hexes_to_gebco',
    python_callable=assign_gebcoTID_hex,
    provide_context=True,
    dag=dag
)

# DAG task dependencies
#download_and_unzip_task >> netcdf_to_pgsql_task >> assign_hexes_to_gebco_task
download_and_unzip_task >> netcdf_to_pgsql_task