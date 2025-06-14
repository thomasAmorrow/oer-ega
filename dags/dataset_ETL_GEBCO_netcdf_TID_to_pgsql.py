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
    zip_path = "/mnt/bucket/gebco_2024.zip"

    response = requests.get(url, stream=True)
    response.raise_for_status()

    with open(zip_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)

    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        zip_ref.extract("GEBCO_2024_TID.nc","/mnt/bucket/")

    logging.info("Extracted files to: /mnt/bucket/")

    # cleanup
    os.remove(zip_path)


def netcdf_to_pgsql(table_name, db_name, db_user, srid, chunk_size=1000):
    """Loads a raster file into a PostgreSQL/PostGIS database using raster2pgsql with chunking."""
    file_path = "/mnt/bucket/GEBCO_2024_TID.nc"
    sql_file_path = "/mnt/bucket/gebco_2024.sql"
    
    logging.info('Creating SQL file...')
    # Create the SQL file using raster2pgsql
    command = f'raster2pgsql -s {srid} -t 256x256 -I -C -c "{file_path}" "{table_name}" > {sql_file_path}'
    
    # Execute raster2pgsql command to generate the SQL file
    subprocess.run(command, shell=True, check=True)

    # Ensure raster extensions exist
    sql_statements="""
    CREATE EXTENSION IF NOT EXISTS postgis;
    CREATE EXTENSION IF NOT EXISTS postgis_raster;

    DROP TABLE IF EXISTS gebco_2024 CASCADE;
    """

     # Initialize PostgresHook
    pg_hook = PostgresHook(postgres_conn_id="oceexp-db")

    try:
        logging.info("Executing SQL statements for raster extension...")
        pg_hook.run(sql_statements, autocommit=True)
        logging.info("SQL execution completed successfully, confirmed raster extension")
    except Exception as e:
        logging.error(f"SQL execution failed: {e}")
        raise

    logging.info('SQL file created, loading...')

    try:
        # Accessing connection details from the hook
        conn = pg_hook.get_conn()
        cursor = conn.cursor()

        # Open and read the SQL file generated by raster2pgsql
        with open(sql_file_path, 'r') as sql_file:
            # Read the file in chunks to avoid memory overload
            chunk = []
            for line in sql_file:
                chunk.append(line)
                
                # If the chunk size is reached, execute the SQL commands
                if len(chunk) >= chunk_size:
                    sql_commands = ''.join(chunk)
                    logging.info(f"Executing chunk of {len(chunk)} commands...")
                    cursor.execute(sql_commands)
                    conn.commit()
                    chunk = []  # Reset chunk

            # Execute any remaining commands in the final chunk
            if chunk:
                sql_commands = ''.join(chunk)
                logging.info(f"Executing final chunk of {len(chunk)} commands...")
                cursor.execute(sql_commands)
                conn.commit()

        logging.info("SQL commands loaded successfully into PostgreSQL!")

    except Exception as e:
        logging.error(f"SQL execution failed: {e}")
        raise
    finally:
        # Ensure cursor and connection are closed
        cursor.close()
        conn.close()

    # cleanup
    os.remove(file_path)
    os.remove(sql_file_path)

def assign_gebcoTID_hex():
    """Assign hexes to GEBCO data."""
    sql_statements = """        
        CREATE TABLE IF NOT EXISTS gebco_2024_polygons (
            polygon_id SERIAL PRIMARY KEY,
            rid INTEGER,
            polygon GEOMETRY,
            val INTEGER
        );

        TRUNCATE gebco_2024_polygons;

        INSERT INTO gebco_2024_polygons (rid, polygon, val)
        SELECT r.rid, d.geom AS polygon, d.val
        FROM gebco_2024 r
        JOIN LATERAL ST_DumpAsPolygons(r.rast) AS d(geom, val) ON true
        WHERE d.val BETWEEN 10 AND 17;

        CREATE EXTENSION IF NOT EXISTS h3;
        CREATE EXTENSION IF NOT EXISTS h3_postgis CASCADE;

        CREATE TABLE IF NOT EXISTS gebco_tid_hex (
            hex_05 H3INDEX PRIMARY KEY,
            val INT,
            polygon_id INT
        );

        TRUNCATE gebco_tid_hex;

        DO $$
        BEGIN
            -- Check if h3_oceans exists
            IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'h3_oceans') THEN
                -- If h3_oceans exists, insert using the condition
                INSERT INTO gebco_tid_hex (hex_05, val, polygon_id)
                SELECT hex_05, val, polygon_id
                FROM gebco_2024_polygons,
                LATERAL h3_polygon_to_cells(polygon, 5) AS hex_05
                WHERE val IN (10, 11, 12, 13, 14, 15, 16, 17)
                    AND hex_05 IN (SELECT hex_05 FROM h3_oceans);

                INSERT INTO gebco_tid_hex (hex_05, val, polygon_id)
                SELECT hex_05, val, polygon_id
                FROM gebco_2024_polygons,
                LATERAL h3_polygon_to_cells(polygon, 5) AS hex_05
                WHERE val NOT IN (10, 11, 12, 13, 14, 15, 16, 17)
                    AND hex_05 IN (SELECT hex_05 FROM h3_oceans);
            ELSE
                -- If h3_oceans does not exist, insert without the condition
                INSERT INTO gebco_tid_hex (hex_05, val, polygon_id)
                SELECT hex_05, val, polygon_id
                FROM gebco_2024_polygons,
                LATERAL h3_polygon_to_cells(polygon, 5) AS hex_05
                WHERE val IN (10, 11, 12, 13, 14, 15, 16, 17);

                INSERT INTO gebco_tid_hex (hex_05, val, polygon_id)
                SELECT hex_05, val, polygon_id
                FROM gebco_2024_polygons,
                LATERAL h3_polygon_to_cells(polygon, 5) AS hex_05
                WHERE val NOT IN (10, 11, 12, 13, 14, 15, 16, 17);
            END IF;
        END $$;

        ALTER TABLE gebco_tid_hex ADD FOREIGN KEY (polygon_id) REFERENCES gebco_2024_polygons (polygon_id);
        ALTER TABLE gebco_2024_polygons ADD FOREIGN KEY (rid) REFERENCES gebco_2024 (rid);

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
download_and_unzip_task >> netcdf_to_pgsql_task >> assign_hexes_to_gebco_task