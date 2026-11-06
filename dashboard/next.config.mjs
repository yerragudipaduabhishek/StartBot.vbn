/** @type {import('next').NextConfig} */
const BACKEND_URL = process.env.BACKEND_URL || "http://127.0.0.1:8000";

const nextConfig = {
  rewrites: async () => [
    {
      source: "/api/:path*",
      destination: `${BACKEND_URL}/:path*`,
    },
  ],
};

export default nextConfig;