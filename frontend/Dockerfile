# The built frontend, served by Nginx, which also reverse-proxies /api and /ws
# to the backend container. Built from the repository root, because it needs
# deploy/security-headers.conf as well as frontend/:
#
#     docker build -f frontend/Dockerfile -t ephemeral-rooms-web .
#
# Node is a build-time dependency only and does not survive into the final
# image, which is the same property the systemd deployment has: the server
# never has Node installed.

# --------------------------------------------------------------- build stage
# Vite 7 requires Node ^20.19 or >=22.12. Alpine is fine here - esbuild
# publishes musl binaries, and nothing from this stage is shipped.
FROM node:22-alpine AS build

WORKDIR /build

# Dependencies first, so a source-only change does not re-run the install.
# `npm ci` and not `npm install`: it installs exactly what package-lock.json
# pins and fails if the two have drifted, which is the point of committing a
# lockfile at all.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/tsconfig.json frontend/vite.config.ts frontend/index.html ./
COPY frontend/src ./src

# `npm run build` is `tsc -b && vite build`, so a type error fails the image
# build rather than shipping. The client stays entirely origin-relative - one
# bundle works behind any hostname - so BUILD_ID below is the only build
# argument, and it is a label, not configuration: nothing behaves differently
# because of it.
# The commit being built, stamped into the bundle and shown in the footer.
# Declared after `npm ci` so a new build id does not invalidate the dependency
# layer - only the build below re-runs.
ARG BUILD_ID=dev
ENV VITE_BUILD_ID=${BUILD_ID}

RUN npm run build

# --------------------------------------------------------------- serve stage
FROM nginx:1.29-alpine

# The site config, and the security headers taken from the same file the
# systemd deployment installs - one source of truth, so the two deployments
# cannot drift apart on something as easy to get wrong as a CSP.
#
# That file includes Strict-Transport-Security, which is correct even though
# this container speaks plain HTTP: browsers are required to ignore an HSTS
# header that did not arrive over a secure transport, so it is inert on
# http://localhost and takes effect once a TLS terminator is in front.
COPY deploy/docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY deploy/security-headers.conf /etc/nginx/snippets/ephemeral-rooms-security.conf

# Catch a syntax error at build time rather than in a crash loop at deploy
# time. The `root` it points at does not exist yet; -t does not care.
RUN nginx -t

COPY --from=build /build/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["wget", "--quiet", "--tries=1", "--spider", "http://127.0.0.1/index.html"]
