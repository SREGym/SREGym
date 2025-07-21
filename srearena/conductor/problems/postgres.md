
# PostgreSQL 17.5 Installation Guide (Mac Homebrew)
Follow these steps to **build PostgreSQL 17.5 from source** on macOS.

## **Step 0. Install dependencies**
brew install gcc make flex bison readline zlib
brew install icu4c
export PKG_CONFIG_PATH="$(brew --prefix icu4c)/lib/pkgconfig"

## **Step 1. Download PostgreSQL source**

curl -OLv https://ftp.postgresql.org/pub/source/v17.5/postgresql-17.5.tar.gz

## **Step 2. Extract the tarbell**
tar xzf postgresql-17.5.tar.gz
cd postgresql-17.5

## **Step 3. Configure the build**
./configure

## **Step 4. Build PostgreSQL**
make

## **Step 5. Initialize the database cluster.**
export PATH=$HOME/pgsql/bin:$PATH
initdb -D $HOME/pgsql/data

## **Step 6. Start the PostgreSQL server**
pg_ctl -D $HOME/pgsql/data -l logfile start

## **Step 7. Create a test database**
createdb test

## **Step 8. Connect to your test database**
psql test

## All of the commands :

```
brew install gcc make flex bison readline zlib
brew install icu4c
export PKG_CONFIG_PATH="$(brew --prefix icu4c)/lib/pkgconfig"
curl -OLv https://ftp.postgresql.org/pub/source/v17.5/postgresql-17.5.tar.gz
tar xzf postgresql-17.5.tar.gz
cd postgresql-17.5
./configure
make
export PATH=$HOME/pgsql/bin:$PATH
initdb -D $HOME/pgsql/data
pg_ctl -D $HOME/pgsql/data -l logfile start
createdb test
psql test 

```
