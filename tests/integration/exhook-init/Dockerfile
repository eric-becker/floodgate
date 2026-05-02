FROM alpine:3.20

RUN apk add --no-cache curl jq bash

COPY register.sh /register.sh
RUN chmod +x /register.sh

ENTRYPOINT ["/register.sh"]
