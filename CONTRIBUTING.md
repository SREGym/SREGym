# Contributing to SREGym
## Table of Contents

- [Contributing to SREGym](#contributing-to-sregym)
  - [Table of Contents](#table-of-contents)
  - [Getting Started](#getting-started)
  - [Development Setup](#development-setup)
    - [Prerequisites](#prerequisites)
    - [Installation](#installation)
  - [How to Contribute](#how-to-contribute)
    - [Reporting Bugs](#reporting-bugs)
    - [Suggesting Features](#suggesting-features)
    - [Code Contributions](#code-contributions)
  - [Development Guidelines](#development-guidelines)
    - [Code Style](#code-style)
      - [Python Code Style](#python-code-style)
      - [Running Code Formatters](#running-code-formatters)
      - [Code Quality Best Practices](#code-quality-best-practices)
    - [Commit Messages](#commit-messages)
  - [Adding New Components](#adding-new-components)
    - [Adding New Applications](#adding-new-applications)
    - [Adding New Problems](#adding-new-problems)
    - [Adding New Oracles](#adding-new-oracles)
  - [Pull Request Process](#pull-request-process)
    - [PR Checklist](#pr-checklist)
  - [Community](#community)
    - [Getting Help](#getting-help)
    - [Staying Updated](#staying-updated)
  - [License](#license)

## Getting Started

Before contributing, please:

1. Read the [README.md](README.md) to understand the project
2. Review the [Problem List](https://docs.google.com/spreadsheets/d/1FGIeLNcKsHjrZGQ_VJcQRGl6oTmYyzjW0_ve5tfM_eg/edit?usp=sharing)
3. Join our [Slack community](https://join.slack.com/t/SREGym/shared_invite/zt-3gvqxpkpc-RvCUcyBEMvzvXaQS9KtS_w)
4. Check existing [issues](https://github.com/xlab-uiuc/SREGym/issues) to avoid duplicates

## Development Setup

### Prerequisites

- Python >= 3.12
- [Helm](https://helm.sh/)
- [brew](https://docs.brew.sh/Homebrew-and-Python)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [uv](https://github.com/astral-sh/uv) (recommended for dependency management)

### Installation

1. Fork the repository on GitHub

2. Clone your fork with submodules:
   ```bash
   git clone --recurse-submodules https://github.com/YOUR_USERNAME/SREGym
   cd SREGym
   ```

3. Install dependencies:
   ```bash
   uv sync
   ```

4. Install pre-commit hooks:
   ```bash
   uv run pre-commit install
   ```

5. Set up your cluster (see [README.md](README.md#🚀quickstart) for options)

## How to Contribute

### Reporting Bugs

When reporting bugs, please include:

- **Clear description** of the issue
- **Steps to reproduce** the problem
- **Expected behavior** vs **actual behavior**
- **Environment details** (OS, Python version, cluster type)
- **Relevant logs or error messages**
- **Problem ID** if the issue is specific to a particular problem

Use the GitHub issue tracker with the "bug" label.

### Suggesting Features

We welcome feature suggestions! Please:

- Check if the feature has already been requested
- Describe the use case and expected behavior
- Explain why this feature would be valuable
- Consider whether it aligns with the project's goals

Use the GitHub issue tracker with the "enhancement" label.

### Code Contributions

We welcome contributions including:

- Bug fixes
- New applications
- New problems
- New oracles or evaluation metrics
- MCP server enhancements
- Documentation improvements
- Test coverage improvements
- Performance optimizations

## Development Guidelines

### Code Style

SREGym follows code style guidelines enforced by pre-commit hooks:

#### Python Code Style

- **Formatter**: [Black](https://github.com/psf/black) with 120 character line length
- **Import sorting**: [isort](https://pycqa.github.io/isort/) with Black profile
- **Target Python version**: 3.12

Configuration is in `pyproject.toml`:
```toml
[tool.black]
line-length = 120
target-version = ["py312"]

[tool.isort]
profile = "black"
line_length = 120
```

#### Running Code Formatters

Pre-commit hooks will automatically run on commit. To manually format:
```bash
# Format all Python files
uv run black .

# Sort imports
uv run isort .

# Run all pre-commit hooks manually
uv run pre-commit run --all-files
```

#### Code Quality Best Practices

- Write clear, self-documenting code
- Add docstrings to classes and functions
- Keep functions focused and modular
- Follow existing patterns in the codebase
- Handle errors gracefully with appropriate logging
- Avoid hardcoded values; use configuration where appropriate

### Commit Messages

Write clear, descriptive commit messages:

- First line should be 50 characters or less
- Provide additional context in the body if needed
- Reference issue numbers when applicable

Example:
```
Add support for custom fault injection parameters

- Extend VirtualizationFaultInjector to accept custom config
- Add validation for parameter types
- Update documentation with examples

Fixes #123
```

## Adding New Components

### Adding New Applications

To add a new application to SREGym:

1. **Create application metadata** in `SREGym/service/metadata/<app-name>.json`:
   ```json
   {
     "name": "My Application",
     "description": "Description of the application",
     "namespace": "my-app",
     "Helm Config": {
       "release_name": "my-app-release",
       "chart_path": "path/to/helm/chart",
       "namespace": "my-app"
     }
   }
   ```

2. **Create application class** in `sregym/service/apps/<app-name>.py`:
   ```python
   from sregym.service.apps.base import Application

   class MyApp(Application):
       def __init__(self):
           super().__init__("sregym/service/metadata/<app-name>.json")
   ```

3. **Update documentation** if needed

### Adding New Problems

To add a new problem:

1. **Create problem file** in `sregym/conductor/problems/<problem-name>.py`:
   ```python
   from sregym.service.apps.myapp import MyApp
   from sregym.conductor.oracles.detection import DetectionOracle
   from sregym.conductor.oracles.localization import LocalizationOracle
   from sregym.conductor.oracles.mitigation import MitigationOracle
   from sregym.conductor.problems.base import Problem
   from sregym.utils.decorators import mark_fault_injected

   class MyProblem(Problem):
       def __init__(self):
           self.app = MyApp()
           self.faulty_service = ["service-name"]

           # Attach evaluation oracles
           self.localization_oracle = LocalizationOracle(
               problem=self,
               expected=self.faulty_service
           )
           self.mitigation_oracle = MitigationOracle(problem=self)

       @mark_fault_injected
       def inject_fault(self):
           # Fault injection logic
           pass

       @mark_fault_injected
       def recover_fault(self):
           # Fault recovery logic
           pass
   ```

2. **Register your problem** in `sregym/conductor/problems/registry.py`

3. **(Optional) Configure tasks** in `sregym/conductor/tasklist.yml`:
   ```yaml
   my-problem-id:
     - detection
     - localization
     - mitigation
   ```

4. **Document the problem** with clear description and expected behavior

### Adding New Oracles

Custom oracles allow for specialized evaluation:

1. Create your oracle in `SREGym/conductor/oracles/<oracle-name>.py`
2. Inherit from appropriate base oracle class
3. Implement evaluation logic
4. Document the oracle's purpose and usage

## Pull Request Process

1. **Create a feature branch** from `main`:
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. **Make your changes** following the development guidelines

3. **Commit your changes** with clear commit messages

4. **Push to your fork**:
   ```bash
   git push origin feature/your-feature-name
   ```

5. **Create a Pull Request** on GitHub:
   - Provide a clear title and description
   - Reference any related issues
   - Explain what changes you made and why
   - Describe how you tested the changes
   - Include screenshots or examples if applicable

6. **Respond to feedback**:
   - Address review comments promptly
   - Update your PR as needed
   - Keep discussions constructive and professional

7. **Wait for approval**:
   - At least one maintainer approval is required
   - All CI checks must pass
   - No merge conflicts with main branch

### PR Checklist

Before submitting, ensure:

- [ ] Code follows the project's style guidelines
- [ ] Documentation updated if needed
- [ ] Pre-commit hooks pass
- [ ] No unnecessary files or changes included
- [ ] Commit messages are clear and descriptive
- [ ] PR description explains the changes

## Community

### Getting Help

- **Slack**: Join our [Slack community](https://join.slack.com/t/SREGym/shared_invite/zt-3gvqxpkpc-RvCUcyBEMvzvXaQS9KtS_w) for discussions
- **Issues**: Use GitHub issues for bug reports and feature requests
- **Documentation**: Check the [README](README.md) and existing documentation

### Staying Updated

- Watch the repository for updates
- Follow discussions on Slack
- Review the problem list for new additions

## License

By contributing to SREGym, you agree that your contributions will be licensed under the [MIT License](LICENSE.txt).

---
