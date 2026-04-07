//basta sa docker ni sya

docker compose up -d db
python3 migrate_data.py
docker compose up -d app

//if nd mag load
docker compose up -d

//check if ga run
docker compose ps


docker compose exec db psql -U user -d viscane_db
viscane_db=#

//show tables 
\dt

SELECT * FROM "user";
SELECT * FROM scan;
SELECT * FROM system_config;

//leave postgresql
\q

