# Contributing to zm_aio_tools (ZM AIO TOOL)

Thank you for your interest in contributing! We welcome bug reports, feature suggestions, and pull requests.

---

## 🛠️ Development Setup

### Prerequisites
- **Node.js**: 18+ (Node 20+ recommended)
- **Python**: 3.10 – 3.12 (Python 3.12 recommended)
- **FFmpeg & FFprobe**: Available in system `PATH`
- **Git**

### Getting Started

1. **Fork and clone the repository**:
   ```bash
   git clone https://github.com/<your-username>/zm_aio_tools.git
   cd zm_aio_tools
   ```

2. **Initialize dependencies**:
   ```bash
   npm run setup
   ```
   *This automatically creates the Python virtual environment in `backend/.venv` and installs frontend and backend dependencies.*

3. **Start the development servers**:
   ```bash
   npm run dev:all
   ```
   - Frontend: `http://127.0.0.1:5173`
   - Backend API: `http://127.0.0.1:8787`

---

## 🧪 Testing & Verification

Before submitting changes, ensure all tests and builds pass:

```bash
# 1. Check UI localization catalog
npm run test:i18n

# 2. Typecheck and build frontend
npm run build

# 3. Run backend tests (optional)
cd backend && .venv/bin/pytest tests/ 2>/dev/null || pytest
```

---

## 📝 Guidelines

### UI Localization (i18n)
- Every user-facing UI element must support both **Vietnamese** and **English**.
- Use `localize(locale, vietnamese, english)` or the local `t(vi, en)` wrapper.
- Do not introduce hardcoded single-language strings in UI components.

### Code Style
- Keep changes focused and minimal.
- Maintain architecture boundaries as outlined in [STRUCTURE.md](STRUCTURE.md).
- Avoid modifying core dependencies without prior discussion.

---

## 🚀 Submitting a Pull Request

1. Create a feature branch from `main`:
   ```bash
   git checkout -b feature/your-feature-name
   ```
2. Commit your changes with clear, descriptive commit messages.
3. Push to your fork and open a Pull Request against the `main` branch.
4. Describe your changes clearly in the PR description, including test steps.
