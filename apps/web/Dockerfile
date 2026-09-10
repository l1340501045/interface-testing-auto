FROM node:22-alpine
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci && chown -R node:node /app
COPY --chown=node:node . .
USER node
EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
