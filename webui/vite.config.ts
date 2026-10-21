import tailwindcss from '@tailwindcss/vite';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';
import { existsSync, copyFileSync } from 'fs';
import { resolve } from 'path';

// If .env doesn't exist, fall back to .env.example (skip in Docker builds)
const envPath = resolve(__dirname, '.env');
const examplePath = resolve(__dirname, '.env.example');
if (!process.env.DOCKER_BUILD && !existsSync(envPath) && existsSync(examplePath)) {
	copyFileSync(examplePath, envPath);
	console.log('No .env found — copied .env.example to .env');
}

// `host: true` binds every interface, not just loopback. SimplyPrint's PHP container
// asks this server whether a contribution's pull request is still open, and a
// loopback-only bind is unreachable from inside Docker - which fails open, so every
// merged contribution stays layered forever with nothing reporting why.
export default defineConfig({ plugins: [tailwindcss(), sveltekit()], server: { port: 5174, host: true } });
