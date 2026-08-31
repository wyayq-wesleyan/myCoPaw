FROM qiuchenjun/highgo:hgdb-ee-6.0.4.1

# The upstream image initializes the highgo role with a hard-coded password.
# Use POSTGRES_PASSWORD instead, matching the rest of its entrypoint contract.
RUN sed -i \
    's|echo "Hello@123456" > ${passwords_file}|printf "%s\\n" "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set}" > "${passwords_file}"|' \
    /usr/local/bin/docker-entrypoint.sh
