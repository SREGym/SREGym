import org.apache.kafka.clients.producer.KafkaProducer;
import org.apache.kafka.clients.producer.ProducerRecord;
import org.apache.kafka.clients.producer.ProducerConfig;
import org.apache.kafka.common.serialization.StringSerializer;
import org.apache.kafka.common.serialization.ByteArraySerializer;
import java.util.Properties;

public class ProducerLeak {
    public static void main(String[] args) throws InterruptedException {
        int count = 0;
        while (true) {
            Properties props = new Properties();
            props.put(ProducerConfig.BOOTSTRAP_SERVERS_CONFIG, "kafka:9092");
            props.put(ProducerConfig.ENABLE_IDEMPOTENCE_CONFIG, true);
            props.put(ProducerConfig.KEY_SERIALIZER_CLASS_CONFIG, StringSerializer.class.getName());
            props.put(ProducerConfig.VALUE_SERIALIZER_CLASS_CONFIG, ByteArraySerializer.class.getName());

            KafkaProducer<String, byte[]> producer = new KafkaProducer<>(props);
            producer.send(new ProducerRecord<>("orders", "order_created".getBytes()));
            producer.flush();
            count++;
            if (count % 100 == 0) {
                System.out.println("Created " + count + " producers");
            }
        }
    }
}