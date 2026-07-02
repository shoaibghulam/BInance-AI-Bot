/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: 'class', // toggled via .dark on <body>
  content: [
    './frontend/index.html',
    './frontend/app.js',
  ],
  safelist: [
    // Belt-and-suspenders for any raw color utility computed at runtime.
    {
      pattern: /^(bg|text|border|fill)-(brand|success|error|warning|gray)-(50|100|400|500|600)$/,
      variants: ['dark', 'hover'],
    },
    'up', 'down', 'muted',
  ],
  theme: {
    extend: {
      colors: {
        brand:   { 25: '#f2f7ff', 50: '#ecf3ff', 100: '#dde9ff', 200: '#c2d6ff', 300: '#9cb9ff', 400: '#7592ff', 500: '#465fff', 600: '#3641f5', 700: '#2a31d8', 800: '#252dae', 900: '#262e89', 950: '#161950' },
        gray:    { 50: '#f9fafb', 100: '#f2f4f7', 200: '#e4e7ec', 300: '#d0d5dd', 400: '#98a2b3', 500: '#667085', 600: '#475467', 700: '#344054', 800: '#1d2939', 900: '#101828' },
        success: { 50: '#ecfdf3', 500: '#12b76a', 600: '#039855' },
        error:   { 50: '#fef3f2', 500: '#f04438', 600: '#d92d20' },
        warning: { 50: '#fffaeb', 500: '#f79009', 600: '#dc6803' },
      },
      fontFamily: { outfit: ['Outfit', 'system-ui', 'sans-serif'] },
      boxShadow: {
        'theme-xs': '0 1px 2px 0 rgba(16,24,40,0.05)',
        'theme-lg': '0 12px 16px -4px rgba(16,24,40,0.08)',
      },
      fontSize: {
        'title-sm': ['1.25rem', { lineHeight: '1.75rem' }],
        'theme-xs': ['0.75rem', { lineHeight: '1rem' }],
        'theme-sm': ['0.875rem', { lineHeight: '1.25rem' }],
      },
    },
  },
  plugins: [],
};
