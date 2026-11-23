# Modeling guidance: seeded-shop

## Goal

Decide whether the shop can be trusted under real traffic: many users, several browser tabs per user,
and clients that retry requests.

## Scope and boundary

The whole HTTP API in `src/server.ts` and the database schema in `prisma/`. The payment step is not
part of this app.

## Prioritized questions

1. Do the guarantees the README states for checkout and restock still hold when requests for the same
   products, coupons or orders run concurrently, or when a client retries a request?

## Interactions to follow

Every endpoint that reads or writes products, orders and coupon redemptions.

## Environment and fault assumptions

One app instance with its default database connection pool, PostgreSQL 17 with its default
configuration. Clients may retry a request whose response they did not receive.
