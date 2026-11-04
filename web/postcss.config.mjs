// web/postcss.config.mjs — AA-1036 (R3 slice 1): Tailwind 4 for the Content studio's stylesheet only
// (app/(studio)/studio.css imports it with source(none): preflight, no utilities). Other CSS files carry no Tailwind
// directive and pass through unchanged.
const config = {
  plugins: {
    "@tailwindcss/postcss": {},
  },
};

export default config;
