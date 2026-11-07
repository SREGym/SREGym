package main

import (
	"context"
	"crypto/hmac"
	"encoding/json"
	"github.com/twmb/franz-go/pkg/kgo"
	"log"
	"net/http"
	"os/signal"
	"shuffle/shared"
	"strings"
	"syscall"
	"time"
)

func normalize(body []byte) ([]byte, error) {
	var document map[string]any
	err := json.Unmarshal(body, &document)
	if err != nil {
		return nil, err
	}
	if data, ok := document["data"].(map[string]any); ok {
		document = data
	}
	if revision, ok := document["Version"].(float64); ok {
		document["Version"] = int64(revision)
	}
	return json.Marshal(document)
}

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	client, err := kgo.NewClient(kgo.SeedBrokers(strings.Split(shared.Env("KAFKA_ADDR", "kafka:9092"), ",")...),
		kgo.MetadataMinAge(100*time.Millisecond), kgo.MetadataMaxAge(time.Second),
		kgo.ConsumeTopics(shared.CommandTopic), kgo.ConsumerGroup("event-gateway"), kgo.DisableAutoCommit(), kgo.BlockRebalanceOnPoll())
	if err != nil {
		log.Fatal(err)
	}
	defer client.CloseAllowingRebalance()
	go func() {
		http.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("ok")) })
		log.Fatal(http.ListenAndServe(":8080", nil))
	}()
	for ctx.Err() == nil {
		fetched := client.PollRecords(ctx, 64)
		if len(fetched.Records()) == 0 {
			time.Sleep(time.Second)
			client.AllowRebalance()
			continue
		}
		var outgoing []*kgo.Record
		for _, record := range fetched.Records() {
			var signature string
			for _, h := range record.Headers {
				if h.Key == "signature" {
					signature = string(h.Value)
				}
			}
			if !hmac.Equal([]byte(signature), []byte(shared.Signature(record.Value))) {
				continue
			}
			body, err := normalize(record.Value)
			if err != nil {
				continue
			}
			outgoing = append(outgoing, &kgo.Record{Topic: shared.Topic, Key: record.Key, Value: body,
				Headers: []kgo.RecordHeader{{Key: "signature", Value: []byte(shared.Signature(body))}}})
		}
		for ctx.Err() == nil && client.ProduceSync(ctx, outgoing...).FirstErr() != nil {
			time.Sleep(100 * time.Millisecond)
		}
		for ctx.Err() == nil && client.CommitRecords(ctx, fetched.Records()...) != nil {
			time.Sleep(time.Second)
		}
		client.AllowRebalance()
	}
}
