package producer

import (
	"database/sql"
	"github.com/IBM/sarama"
	_ "github.com/jackc/pgx/v5/stdlib"
	"log"
	"os"
	"shuffle/shared"
	"strings"
	"time"
)

func Run(kind string) {
	pool, err := sql.Open("pgx", os.Getenv("DATABASE_URL"))
	if err != nil {
		log.Fatal(err)
	}
	defer pool.Close()
	cfg := sarama.NewConfig()
	cfg.Metadata.RefreshFrequency = time.Second
	cfg.Producer.Return.Successes = true
	cfg.Producer.RequiredAcks = sarama.WaitForAll
	client, err := sarama.NewSyncProducer(strings.Split(shared.Env("KAFKA_ADDR", "kafka:9092"), ","), cfg)
	if err != nil {
		log.Fatal(err)
	}
	defer client.Close()
	shared.Serve(kind, func(e *shared.Event) error {
		return pool.QueryRow("SELECT nextval('shop.revisions')").Scan(&e.Version)
	}, func(e shared.Event, body []byte) error {
		_, _, err := client.SendMessage(&sarama.ProducerMessage{Topic: shared.CommandTopic, Key: sarama.StringEncoder(e.Order),
			Value: sarama.ByteEncoder(body), Headers: []sarama.RecordHeader{{Key: []byte("signature"), Value: []byte(shared.Signature(body))}}})
		return err
	})
}
