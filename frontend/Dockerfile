FROM node:18-alpine AS builder

WORKDIR /app

# Copy package files
COPY frontend/package*.json .

# Install dependencies
RUN npm ci

# Copy source code
COPY frontend/ .

# Build the application
RUN npm run build

# Production stage
FROM node:18-alpine

WORKDIR /app

# Install serve to run the built app
RUN npm install -g serve

# Copy built application from builder
COPY --from=builder /app/dist ./dist
COPY --from=builder /app/package*.json .

# Install production dependencies only
RUN npm ci --only=production

# Expose port
EXPOSE 5173

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD wget --quiet --tries=1 --spider http://localhost:5173 || exit 1

# Run the application
CMD ["serve", "-s", "dist", "-l", "5173"]
