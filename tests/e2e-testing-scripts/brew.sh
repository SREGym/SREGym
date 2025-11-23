/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
echo >> $HOME/.bashrc
echo 'eval "$(/home/linuxbrew/.linuxbrew/bin/brew shellenv)"' >> $HOME/.bashrc
eval "$(/home/linuxbrew/.linuxbrew/bin/brew shellenv)"

sudo apt install build-essential -y
brew install gcc