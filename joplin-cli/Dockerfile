FROM node:20-alpine

RUN apk add --no-cache curl sqlite && \
    npm install -g @joplin/cli

# Joplin stores its data in ~/.config/joplin
RUN mkdir -p /root/.config/joplin

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 41184

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -sf http://localhost:41184/ping || exit 1

ENTRYPOINT ["/entrypoint.sh"]
