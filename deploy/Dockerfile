FROM python@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea AS build
WORKDIR /app
COPY deploy/requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --require-hashes --prefix=/install -r /app/requirements.lock

FROM python@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
RUN groupadd -g 10001 relay && useradd -r -u 10001 -g relay relay && install -d -o relay -g relay -m 0700 /var/lib/relay /var/backups/relay
COPY --from=build /install /usr/local
COPY relay_core /app/relay_core
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
WORKDIR /app
ENTRYPOINT ["python", "-m", "relay_core.operations"]
CMD ["api"]
