ARG BUILD_FROM
FROM $BUILD_FROM

WORKDIR /app
COPY rootfs/app/ /app/
COPY run.sh /run.sh
RUN chmod 0755 /run.sh && python3 -m compileall -q /app

EXPOSE 8099
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:8099/health || exit 1
CMD ["/run.sh"]
