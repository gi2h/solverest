# Fortress Captcha

Multi-layer captcha solving architecture for Linux headless servers.

## Services

| Service | Port |
|---------|------|
| api-gateway | 8080 |
| cloak-service | 9001 |
| vision-service | 9002 |
| ocr-service | 9003 |
| wasm-service | 9004 |
| altcha-service | 9005 |
| friendly-service | 9006 |
| router-service | 9007 |

## Quick Start

    cp .env.example .env
    make install
    make models    # download AI models (skip on Termux)
    make up

## License

MIT
